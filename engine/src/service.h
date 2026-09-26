#pragma once

#include <grpcpp/grpcpp.h>

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

private:
    std::int64_t now_or(std::int64_t client_ns) const;

    EngineLoop& loop_;
    MarketValidator validator_;
};

}  // namespace slipstream
