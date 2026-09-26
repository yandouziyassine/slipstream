#include "engine_loop.h"

#include <algorithm>

namespace slipstream {
namespace {

// Only a safety net: commands and close() wake the engine thread directly.
constexpr std::chrono::milliseconds kIdlePoll{1};

std::int64_t steady_now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

}  // namespace

EngineLoop::EngineLoop(Engine& engine, ClockMode mode, std::size_t market_capacity,
                       std::size_t subscriber_capacity,
                       std::chrono::milliseconds replay_push_timeout)
    : engine_(engine),
      mode_(mode),
      subscriber_capacity_(subscriber_capacity),
      replay_push_timeout_(replay_push_timeout),
      market_(market_capacity),
      commands_(kCommandCapacity) {
    engine_thread_ = std::thread([this] { engine_main(); });
    engine_thread_id_ = engine_thread_.get_id();
    if (mode_ != ClockMode::Live) return;
    try {
        timer_thread_ = std::thread([this] { timer_main(); });
    } catch (...) {
        stop();
        throw;
    }
}

EngineLoop::~EngineLoop() { stop(); }

bool EngineLoop::push(MarketItem item) {
    if (mode_ == ClockMode::Replay) {
        return market_.push_for(Slot{Pending{std::move(item), steady_now_ns()}},
                                replay_push_timeout_);
    }
    item.ingest_ns = clock_.now_ns();
    return market_.try_push(Slot{Pending{std::move(item), steady_now_ns()}});
}

bool EngineLoop::bind_venue(std::size_t venue) {
    std::lock_guard lock(bind_mutex_);
    return bound_venues_.insert(venue).second;
}

void EngineLoop::unbind_venue(std::size_t venue) {
    std::lock_guard lock(bind_mutex_);
    bound_venues_.erase(venue);
}

bool EngineLoop::bind_replay() {
    std::lock_guard lock(bind_mutex_);
    if (replay_bound_) return false;
    replay_bound_ = true;
    return true;
}

void EngineLoop::unbind_replay() {
    std::lock_guard lock(bind_mutex_);
    replay_bound_ = false;
}

std::shared_ptr<Subscriber> EngineLoop::subscribe() {
    std::lock_guard lock(subscriber_mutex_);
    if (subscriber_) return nullptr;
    subscriber_ = std::make_shared<Subscriber>(subscriber_capacity_);
    // stop() sets queues_closed_ before it closes the current subscriber under this mutex.
    if (queues_closed_.load()) subscriber_->close();
    return subscriber_;
}

void EngineLoop::unsubscribe(const std::shared_ptr<Subscriber>& subscriber) {
    std::lock_guard lock(subscriber_mutex_);
    if (!subscriber || subscriber_ != subscriber) return;
    subscriber_->close();
    subscriber_.reset();
}

LoopStats EngineLoop::stats() {
    return inspect([](Engine&, const LoopView& view) { return view.stats; });
}

SubmitResult EngineLoop::submit_and_step(ParentOrderRequest request, ScheduleSpec spec) {
    return run([this, request = std::move(request), spec = std::move(spec)](Engine& engine) {
        auto timed = request;
        if (mode_ == ClockMode::Live) timed.start_ns = std::max(clock_.now_ns(), event_time_);
        auto result = engine.submit(timed, spec);
        if (result.accepted) {
            event_time_ = std::max(event_time_, timed.start_ns);
            step_if_subscribed();
        }
        return result;
    });
}

LoopView EngineLoop::view() const {
    const std::int64_t now = mode_ == ClockMode::Live ? clock_.now_ns() : event_time_;
    return {now, LoopStats{events_, market_.high_water(), latency_.percentile(50),
                           latency_.percentile(99)}};
}

void EngineLoop::stop() {
    {
        std::lock_guard lock(stop_mutex_);
        if (stopping_) return;
        stopping_ = true;
    }
    stop_cv_.notify_all();
    if (timer_thread_.joinable()) timer_thread_.join();
    commands_.close();
    market_.close();
    queues_closed_.store(true);
    engine_thread_.join();
    std::lock_guard lock(subscriber_mutex_);
    if (subscriber_) subscriber_->close();
}

void EngineLoop::post(Command command) {
    if (!commands_.try_push(std::move(command))) {
        std::lock_guard lock(stop_mutex_);
        throw std::runtime_error(stopping_ ? "engine loop stopped"
                                           : "engine loop command queue full");
    }
    // An idle engine thread is blocked on the market queue. A non-empty queue needs no wake:
    // the engine thread drains commands again right after its next item.
    if (market_.size() == 0) market_.try_push(Slot{Wake{}});
}

void EngineLoop::engine_main() {
    for (;;) {
        // Read before draining: once the queues are closed, nothing new can arrive, so an empty
        // pop after this drain means everything was processed.
        const bool closed = queues_closed_.load();
        drain_commands();
        const auto slot = market_.pop_for(kIdlePoll);
        if (slot) {
            if (const auto* pending = std::get_if<Pending>(&*slot)) process(*pending);
            continue;
        }
        if (closed) return;
    }
}

void EngineLoop::timer_main() {
    std::unique_lock lock(stop_mutex_);
    while (!stop_cv_.wait_for(lock, kTickInterval, [this] { return stopping_; })) {
        lock.unlock();
        push(MarketItem{0, TickData{clock_.now_ns()}, 0});  // a full queue skips this tick
        lock.lock();
    }
}

void EngineLoop::drain_commands() {
    while (auto command = commands_.try_pop()) (*command)(engine_);
}

void EngineLoop::process(const Pending& pending) {
    event_time_ = std::max(event_time_, item_time(pending.item));
    apply(pending.item);
    step_if_subscribed();
    ++events_;
    latency_.record(steady_now_ns() - pending.ingest_steady_ns);
}

std::int64_t EngineLoop::item_time(const MarketItem& item) const {
    if (mode_ == ClockMode::Live) return item.ingest_ns;
    if (const auto* book = std::get_if<BookData>(&item.data)) return book->recv_ns;
    if (const auto* tick = std::get_if<TickData>(&item.data)) return tick->now_ns;
    return event_time_;  // replay heartbeats and trades carry no time
}

void EngineLoop::apply(const MarketItem& item) {
    // Ingest validated the item; the engine re-checks and leaves its state unchanged on reject.
    if (const auto* book = std::get_if<BookData>(&item.data)) {
        if (book->is_snapshot) {
            engine_.apply_book_snapshot(item.venue, book->bids, book->asks, event_time_);
        } else {
            engine_.apply_book_update(item.venue, book->bids, book->asks, event_time_);
        }
    } else if (const auto* trades = std::get_if<TradeData>(&item.data)) {
        engine_.apply_trades(trades->trades);
    } else if (std::holds_alternative<HeartbeatData>(item.data)) {
        engine_.apply_heartbeat(item.venue, event_time_);
    }
}

// Until PR 3 removes the unary Step RPC, a client without a subscription steps orders itself
// and reads the fills from StepReply, so the loop must not step (and consume) them first.
void EngineLoop::step_if_subscribed() {
    std::shared_ptr<Subscriber> subscriber;
    {
        std::lock_guard lock(subscriber_mutex_);
        subscriber = subscriber_;
    }
    if (subscriber) publish(*subscriber, engine_.step(event_time_));
}

void EngineLoop::publish(Subscriber& subscriber, StepOutput output) {
    const auto emit = [&](std::variant<Fill, OrderUpdate> event) {
        subscriber.publish(EngineEventData{next_seq_++, std::move(event)});
    };
    for (auto& fill : output.fills) emit(std::move(fill));
    for (auto& update : output.updates) emit(std::move(update));
}

}  // namespace slipstream
