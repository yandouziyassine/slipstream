#pragma once

#include <grpcpp/grpcpp.h>
#include <gtest/gtest.h>

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <thread>

#include "engine_loop.h"
#include "service.h"

namespace slipstream::test {

// Two zero-fee venues, "kraken" (0) and "coinbase" (1), book depth 10.
inline constexpr std::int64_t kStaleNs = 2'000'000'000;

// A real gRPC server in this process, reached through its in-process channel.
class Harness {
public:
    explicit Harness(ClockMode mode, std::size_t market_capacity = 10'000,
                     std::size_t subscriber_capacity = 10'000,
                     std::chrono::milliseconds replay_push_timeout = EngineLoop::kReplayPushTimeout)
        : engine_(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 0.0}, {"coinbase", 0.0}},
                  kStaleNs),
          loop_(engine_, mode, market_capacity, subscriber_capacity, replay_push_timeout),
          service_(loop_, "BTC/USD") {
        grpc::ServerBuilder builder;
        builder.RegisterService(&service_);
        server_ = builder.BuildAndStart();
        stub_ = v1::ExecutionEngine::NewStub(server_->InProcessChannel(grpc::ChannelArguments{}));
    }

    ~Harness() {
        server_->Shutdown(std::chrono::system_clock::now() + std::chrono::seconds(2));
        loop_.stop();
    }

    Harness(const Harness&) = delete;
    Harness& operator=(const Harness&) = delete;

    v1::ExecutionEngine::Stub& stub() { return *stub_; }
    EngineLoop& loop() { return loop_; }

    v1::StatusReply status() {
        grpc::ClientContext context;
        v1::StatusReply reply;
        EXPECT_TRUE(stub_->GetStatus(&context, v1::StatusRequest{}, &reply).ok());
        return reply;
    }

    // Polls GetStatus: commands run ahead of queued market items, so one call is not a barrier.
    bool eventually(const std::function<bool(const v1::StatusReply&)>& done,
                    std::chrono::seconds timeout = std::chrono::seconds(10)) {
        const auto deadline = std::chrono::steady_clock::now() + timeout;
        while (std::chrono::steady_clock::now() < deadline) {
            if (done(status())) return true;
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
        return false;
    }

    v1::SubmitReply submit(const std::string& id, std::int64_t start_ns, std::int64_t duration_ns,
                           int slices) {
        v1::ParentOrder order;
        order.set_order_id(id);
        order.set_side(v1::SIDE_BUY);
        order.set_qty(1.0);
        order.set_start_ns(start_ns);
        order.set_duration_ns(duration_ns);
        order.set_num_slices(slices);
        grpc::ClientContext context;
        v1::SubmitReply reply;
        EXPECT_TRUE(stub_->SubmitParentOrder(&context, order, &reply).ok());
        return reply;
    }

private:
    Engine engine_;
    EngineLoop loop_;
    ExecutionService service_;
    std::unique_ptr<grpc::Server> server_;
    std::unique_ptr<v1::ExecutionEngine::Stub> stub_;
};

class Stream {
public:
    Stream(v1::ExecutionEngine::Stub& stub, const std::optional<std::string>& venue,
           std::chrono::seconds deadline = std::chrono::seconds(30)) {
        context_.set_deadline(std::chrono::system_clock::now() + deadline);
        if (venue) context_.AddMetadata("slipstream-venue", *venue);
        writer_ = stub.MarketStream(&context_, &summary_);
    }

    // The result is ignored on purpose: after the server ends the call, writes just fail and
    // finish() reports the status.
    void send(const v1::MarketEvent& event) { (void)writer_->Write(event); }

    grpc::Status finish() {
        (void)writer_->WritesDone();
        return writer_->Finish();
    }

    std::uint64_t events() const { return summary_.events(); }

private:
    grpc::ClientContext context_;
    v1::MarketStreamSummary summary_;
    std::unique_ptr<grpc::ClientWriter<v1::MarketEvent>> writer_;
};

class Subscription {
public:
    explicit Subscription(v1::ExecutionEngine::Stub& stub) {
        context_.set_deadline(std::chrono::system_clock::now() + std::chrono::seconds(30));
        reader_ = stub.Subscribe(&context_, v1::SubscribeRequest{});
    }

    // The server sends initial metadata once the subscription is active.
    void wait_active() { reader_->WaitForInitialMetadata(); }

    std::optional<v1::EngineEvent> next() {
        v1::EngineEvent event;
        if (!reader_->Read(&event)) return std::nullopt;
        return event;
    }

    grpc::Status finish() { return reader_->Finish(); }

    grpc::Status cancel() {
        context_.TryCancel();
        while (next()) {
        }
        return finish();
    }

private:
    grpc::ClientContext context_;
    std::unique_ptr<grpc::ClientReader<v1::EngineEvent>> reader_;
};

}  // namespace slipstream::test
