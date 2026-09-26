#pragma once

#include <grpcpp/grpcpp.h>

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>

#include "engine_loop.h"
#include "market_validation.h"
#include "slipstream/v1/execution.grpc.pb.h"

namespace slipstream {

// Every call reaches the engine through the loop. In live mode client-supplied times (recv_ns,
// now_ns, start_ns) are ignored and replaced by the engine clock.
class ExecutionService final : public v1::ExecutionEngine::Service {
public:
    ExecutionService(EngineLoop& loop, std::string symbol);

    grpc::Status ApplyBookUpdate(grpc::ServerContext*, const v1::BookUpdate* request,
                                 v1::BookAck*) override;
    grpc::Status SubmitParentOrder(grpc::ServerContext*, const v1::ParentOrder* request,
                                   v1::SubmitReply* reply) override;
    grpc::Status Step(grpc::ServerContext*, const v1::StepRequest* request,
                      v1::StepReply* reply) override;
    grpc::Status GetStatus(grpc::ServerContext*, const v1::StatusRequest*,
                           v1::StatusReply* reply) override;
    grpc::Status ApplyTrades(grpc::ServerContext*, const v1::TradeBatch* request,
                             v1::TradeAck*) override;
    // Live: one stream per venue, named by the kVenueMetadataKey request metadata.
    // Replay: one stream at a time. Ends with INVALID_ARGUMENT on the first invalid event and
    // RESOURCE_EXHAUSTED when the engine queue is full (replay: still full after
    // EngineLoop's replay wait).
    grpc::Status MarketStream(grpc::ServerContext* context,
                              grpc::ServerReader<v1::MarketEvent>* reader,
                              v1::MarketStreamSummary* summary) override;
    // Initial metadata is sent once the subscription is active, so a client that waits for it
    // receives every event from then on.
    grpc::Status Subscribe(grpc::ServerContext* context, const v1::SubscribeRequest*,
                           grpc::ServerWriter<v1::EngineEvent>* writer) override;

    static constexpr const char* kVenueMetadataKey = "slipstream-venue";
    static constexpr std::chrono::milliseconds kSubscribePoll{50};

private:
    // The venue named by exactly one non-empty kVenueMetadataKey entry.
    std::optional<std::size_t> metadata_venue(const grpc::ServerContext& context) const;
    // Validates and queues events until the client finishes or one is rejected.
    grpc::Status pump(StreamAdmission admission, grpc::ServerReader<v1::MarketEvent>& reader,
                      v1::MarketStreamSummary& summary);
    std::int64_t now_or(std::int64_t client_ns) const;

    EngineLoop& loop_;
    MarketValidator validator_;
};

}  // namespace slipstream
