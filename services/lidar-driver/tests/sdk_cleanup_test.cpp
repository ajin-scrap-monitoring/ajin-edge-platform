// Characterize the pinned SDK's queued-buffer cleanup after a stopped receiver.
// Uses real SDK allocation and cleanup; no sensor or worker threads are needed.
#include "sdkcommon.h"
#include "hal/abs_rxtx.h"
#include "hal/thread.h"
#include "hal/types.h"
#include "hal/assert.h"
#include "hal/locker.h"
#include "hal/socket.h"
#include "hal/event.h"
#include "sl_async_transceiver.h"
#include "sl_lidar_driver.h"
#include <iostream>
#include <memory>
#include <stdexcept>

class UnusedCodec final : public sl::internal::IAsyncProtocolCodec {
public:
    void onDecodeData(const void*, size_t) override {}
    size_t estimateLength(sl::internal::message_autoptr_t&) override { return 0; }
    void onEncodeData(sl::internal::message_autoptr_t&, _u8*, size_t*) override {}
};

class PendingReceiver final : public sl::internal::AsyncTransceiver {
public:
    using AsyncTransceiver::AsyncTransceiver;
    void seed(sl::IChannel* channel, int count) {
        _bindedChannel = channel;
        _isWorking = true;
        for (int i = 0; i < count; ++i) {
            auto* buffer = new Buffer();
            buffer->data = new _u8[32];
            buffer->size = 32;
            _rxQueue.push_back(buffer);
        }
    }
    bool empty() const { return _rxQueue.empty(); }
};

int main() {
    UnusedCodec codec;
    for (int count : {0, 1, 8}) {
        auto result = sl::createUdpChannel("127.0.0.1", 8089);
        if (!result) throw std::runtime_error("channel allocation failed");
        std::unique_ptr<sl::IChannel> channel(*result);
        PendingReceiver receiver(codec);
        receiver.seed(channel.get(), count);
        receiver.unbindAndClose();
        if (!receiver.empty() || receiver.getBindedChannel() != nullptr)
            throw std::runtime_error("receiver did not release pending state");
        receiver.unbindAndClose(); // Repeated disconnect and destructor must be safe.
    }
    std::cout << "queued-buffer cleanup and repeated disconnect passed\n";
}
