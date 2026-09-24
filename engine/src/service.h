#pragma once

#include <grpcpp/grpcpp.h>

#include <string>

#include "engine.h"
#include "slipstream/v1/execution.grpc.pb.h"

namespace slipstream {

class ExecutionService final : public v1::ExecutionEngine::Service {
public:
    static constexpr int kMaxLevelsPerUpdate = 1000;

    ExecutionService(Engine& engine, std::string symbol);

    grpc::Status ApplyBookUpdate(grpc::ServerContext*, const v1::BookUpdate* request,
                                 v1::BookAck*) override;
    grpc::Status SubmitParentOrder(grpc::ServerContext*, const v1::ParentOrder* request,
                                   v1::SubmitReply* reply) override;
    grpc::Status Step(grpc::ServerContext*, const v1::StepRequest* request,
                      v1::StepReply* reply) override;
    grpc::Status GetStatus(grpc::ServerContext*, const v1::StatusRequest*,
                           v1::StatusReply* reply) override;

private:
    Engine& engine_;
    std::string symbol_;
};

}  // namespace slipstream
