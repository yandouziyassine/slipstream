#include <grpcpp/grpcpp.h>

#include <atomic>
#include <chrono>
#include <csignal>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

#include "config.h"
#include "engine.h"
#include "service.h"

namespace {

std::atomic<bool> g_stop{false};

void handle_signal(int) { g_stop.store(true); }

}  // namespace

int main(int argc, char** argv) {
    const std::vector<std::string> args(argv + 1, argv + argc);
    const auto parsed = slipstream::parse_args(args);
    if (!parsed.config) {
        std::cerr << "error: " << parsed.error << '\n';
        return 2;
    }
    const auto& config = *parsed.config;

    slipstream::Engine engine(config.limits, config.book_depth);
    slipstream::ExecutionService service(engine, config.symbol);

    int bound_port = 0;
    grpc::ServerBuilder builder;
    builder.AddListeningPort(config.listen_address, grpc::InsecureServerCredentials(), &bound_port);
    builder.SetMaxReceiveMessageSize(1 << 20);
    builder.RegisterService(&service);
    const auto server = builder.BuildAndStart();
    if (!server || bound_port == 0) {
        std::cerr << "error: failed to listen on " << config.listen_address << '\n';
        return 1;
    }

    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);
    std::cout << "slipstream engine listening on port " << bound_port << " (paper mode, symbol "
              << config.symbol << ")" << std::endl;

    while (!g_stop.load()) std::this_thread::sleep_for(std::chrono::milliseconds(100));
    server->Shutdown();
    return 0;
}
