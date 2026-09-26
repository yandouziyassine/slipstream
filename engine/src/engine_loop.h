#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <future>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <stdexcept>
#include <thread>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

#include "bounded_queue.h"
#include "clock.h"
#include "config.h"
#include "engine.h"
#include "latency_histogram.h"
#include "types.h"

namespace slipstream {

struct BookData {
    bool is_snapshot;
    std::vector<Level> bids;
    std::vector<Level> asks;
    std::int64_t recv_ns;
};

struct TradeData {
    std::vector<Trade> trades;
};

struct HeartbeatData {};

struct TickData {
    std::int64_t now_ns;
};

// venue is ignored for trades and ticks. In live mode push() overwrites ingest_ns with the
// engine clock, so the caller's value only matters in replay mode, where it is not used.
struct MarketItem {
    std::size_t venue;
    std::variant<BookData, TradeData, HeartbeatData, TickData> data;
    std::int64_t ingest_ns;
};

struct EngineEventData {
    std::uint64_t seq;
    std::variant<Fill, OrderUpdate> event;
};

struct LoopStats {
    std::uint64_t events;
    std::size_t queue_high_water;
    std::int64_t p50_ns;
    std::int64_t p99_ns;
};

// The loop's current time (live: the engine clock; replay: the latest event time) and stats.
struct LoopView {
    std::int64_t now_ns;
    LoopStats stats;
};

class Subscriber {
public:
    explicit Subscriber(std::size_t capacity) : queue_(capacity) {}

    std::optional<EngineEventData> pop_for(std::chrono::microseconds timeout) {
        return queue_.pop_for(timeout);
    }
    // Once the queue was full the subscriber receives nothing more; its stream must end.
    bool overflowed() const { return overflowed_.load(); }
    // Set when it was unsubscribed or the loop stopped. Events already queued can still be popped.
    bool closed() const { return closed_.load(); }

private:
    friend class EngineLoop;

    void publish(EngineEventData event) {
        if (overflowed_.load()) return;
        if (!queue_.try_push(std::move(event))) overflowed_.store(true);
    }
    void close() {
        closed_.store(true);
        queue_.close();
    }

    BoundedQueue<EngineEventData> queue_;
    std::atomic<bool> overflowed_{false};
    std::atomic<bool> closed_{false};
};

// The single writer: only the engine thread touches the Engine. Market items arrive on a bounded
// queue from any thread; commands (run) go on a separate queue that the engine thread always
// drains before its next market item. run() and stop() must never be called from inside a
// command: run() throws std::logic_error there, and stop() would join its own thread.
class EngineLoop {
public:
    static constexpr std::chrono::milliseconds kTickInterval{50};
    static constexpr std::size_t kCommandCapacity = 1024;

    EngineLoop(Engine& engine, ClockMode mode, std::size_t market_capacity = 10'000,
               std::size_t subscriber_capacity = 10'000);
    ~EngineLoop();

    EngineLoop(const EngineLoop&) = delete;
    EngineLoop& operator=(const EngineLoop&) = delete;

    // False when the queue is full or the loop stopped.
    bool push(MarketItem item);

    // Runs fn(engine) on the engine thread and returns its result or rethrows its exception.
    // Throws std::runtime_error("engine loop stopped") after stop().
    template <class F>
    std::invoke_result_t<F&, Engine&> run(F&& fn);

    // Like run, but fn(engine, view) also sees the loop's time and stats, all in one snapshot.
    template <class F>
    std::invoke_result_t<F&, Engine&, const LoopView&> inspect(F&& fn);

    // Submits on the engine thread. Live mode replaces start_ns with the engine clock. While a
    // subscriber is active, the order is stepped at once so its first slice executes at
    // submission and the subscriber receives those events.
    SubmitResult submit_and_step(ParentOrderRequest request, ScheduleSpec spec);

    ClockMode mode() const { return mode_; }

    bool bind_venue(std::size_t venue);
    void unbind_venue(std::size_t venue);
    bool bind_replay();
    void unbind_replay();

    // nullptr while another subscriber is active.
    std::shared_ptr<Subscriber> subscribe();
    void unsubscribe(const std::shared_ptr<Subscriber>& subscriber);

    LoopStats stats();
    std::int64_t live_now_ns() const { return clock_.now_ns(); }

    // Idempotent. Processes everything already queued, then joins both threads.
    void stop();

private:
    struct Wake {};
    struct Pending {
        MarketItem item;
        std::int64_t ingest_steady_ns;
    };
    using Slot = std::variant<Wake, Pending>;
    using Command = std::function<void(Engine&)>;

    void post(Command command);
    void engine_main();
    void timer_main();
    void drain_commands();
    void process(const Pending& pending);
    std::int64_t item_time(const MarketItem& item) const;
    void apply(const MarketItem& item);
    void step_if_subscribed();
    void publish(Subscriber& subscriber, StepOutput output);
    LoopView view() const;

    Engine& engine_;
    const ClockMode mode_;
    const std::size_t subscriber_capacity_;
    const LiveClock clock_;
    BoundedQueue<Slot> market_;
    BoundedQueue<Command> commands_;

    // Engine thread only.
    std::int64_t event_time_ = 0;
    std::uint64_t next_seq_ = 1;
    std::uint64_t events_ = 0;
    LatencyHistogram latency_;

    std::mutex bind_mutex_;
    std::set<std::size_t> bound_venues_;
    bool replay_bound_ = false;

    std::mutex subscriber_mutex_;
    std::shared_ptr<Subscriber> subscriber_;

    std::mutex stop_mutex_;
    std::condition_variable stop_cv_;
    bool stopping_ = false;
    std::atomic<bool> queues_closed_{false};

    std::thread::id engine_thread_id_;
    std::thread engine_thread_;
    std::thread timer_thread_;
};

template <class F>
std::invoke_result_t<F&, Engine&> EngineLoop::run(F&& fn) {
    using Result = std::invoke_result_t<F&, Engine&>;
    if (std::this_thread::get_id() == engine_thread_id_) {
        throw std::logic_error("EngineLoop::run called from the engine thread");
    }
    auto task = std::make_shared<std::packaged_task<Result(Engine&)>>(std::forward<F>(fn));
    auto result = task->get_future();
    post([task](Engine& engine) { (*task)(engine); });
    return result.get();
}

template <class F>
std::invoke_result_t<F&, Engine&, const LoopView&> EngineLoop::inspect(F&& fn) {
    return run([this, &fn](Engine& engine) { return fn(engine, view()); });
}

}  // namespace slipstream
