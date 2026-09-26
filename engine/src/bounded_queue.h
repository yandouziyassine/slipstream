#pragma once

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <deque>
#include <mutex>
#include <optional>
#include <utility>

namespace slipstream {

// A bounded multi-producer, single-consumer FIFO queue. try_push fails once the queue is at
// capacity or closed; pop_for and try_pop keep draining whatever is left after close().
template <class T>
class BoundedQueue {
public:
    explicit BoundedQueue(std::size_t capacity) : capacity_(capacity) {}

    BoundedQueue(const BoundedQueue&) = delete;
    BoundedQueue& operator=(const BoundedQueue&) = delete;

    bool try_push(T item) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (closed_ || items_.size() >= capacity_) return false;
        items_.push_back(std::move(item));
        if (items_.size() > high_water_) high_water_ = items_.size();
        not_empty_.notify_one();
        return true;
    }

    std::optional<T> pop_for(std::chrono::microseconds timeout) {
        std::unique_lock<std::mutex> lock(mutex_);
        not_empty_.wait_for(lock, timeout, [this] { return !items_.empty() || closed_; });
        return take_front_locked();
    }

    std::optional<T> try_pop() {
        std::lock_guard<std::mutex> lock(mutex_);
        return take_front_locked();
    }

    // Wakes every waiter. Later try_push calls fail, but pop_for/try_pop keep draining items_
    // until it is empty.
    void close() {
        std::lock_guard<std::mutex> lock(mutex_);
        closed_ = true;
        not_empty_.notify_all();
    }

    std::size_t size() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return items_.size();
    }

    std::size_t high_water() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return high_water_;
    }

private:
    // Requires mutex_ to be held by the caller.
    std::optional<T> take_front_locked() {
        if (items_.empty()) return std::nullopt;
        T front = std::move(items_.front());
        items_.pop_front();
        return front;
    }

    mutable std::mutex mutex_;
    std::condition_variable not_empty_;
    std::deque<T> items_;
    std::size_t capacity_;
    std::size_t high_water_ = 0;
    bool closed_ = false;
};

}  // namespace slipstream
