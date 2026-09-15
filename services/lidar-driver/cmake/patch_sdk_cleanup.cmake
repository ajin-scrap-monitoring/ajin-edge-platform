# Keep the upstream checkout immutable. Compile a corrected copy of this one TU.
file(READ "${SDK}/src/sl_async_transceiver.cpp" sdk_transceiver)
string(REPLACE "\r\n" "\n" sdk_transceiver "${sdk_transceiver}")
string(SHA256 sdk_transceiver_sha "${sdk_transceiver}")
if(NOT sdk_transceiver_sha STREQUAL "0f1acab812daec1c1b4929a306fc595b79c8208eabf12451fdad7a71248a082b")
  message(FATAL_ERROR "SLAMTEC transceiver source changed; review the cleanup patch before building")
endif()
# _rxQueue stores individually allocated Buffer objects, whose destructor frees data[].
string(REPLACE "delete [] *itr;" "delete *itr;" sdk_transceiver "${sdk_transceiver}")
set(SDK_TRANSCEIVER "${CMAKE_CURRENT_BINARY_DIR}/sdk-patched/sl_async_transceiver.cpp")
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/sdk-patched")
file(CONFIGURE OUTPUT "${SDK_TRANSCEIVER}" CONTENT "${sdk_transceiver}" @ONLY)
