#pragma once
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <algorithm>
namespace ajin {
struct Sample { uint32_t angle_mdeg,distance_mm,quality; };
inline Sample convert(uint16_t angle_q14,uint32_t distance_q2,uint8_t quality) {
 return {uint32_t((uint64_t(angle_q14)*90000+8192)/16384)%360000,distance_q2/4,uint32_t(quality>>2)};
}
template<class T> class LatestTwo {
 std::mutex mutex_; uint64_t serial_=0;
 std::deque<std::pair<uint64_t,std::shared_ptr<const T>>> frames_;
public:
 void push(std::shared_ptr<const T> frame) {std::lock_guard<std::mutex> lock(mutex_);frames_.emplace_back(++serial_,std::move(frame));if(frames_.size()>2)frames_.pop_front();}
 std::shared_ptr<const T> next(uint64_t& cursor,uint64_t& lost) {
  std::lock_guard<std::mutex> lock(mutex_);
  if(frames_.empty()||cursor>=serial_)return {};
  if(cursor+1<frames_.front().first){lost+=frames_.front().first-cursor-1;cursor=frames_.front().first-1;}
  for(auto& frame:frames_)if(frame.first>cursor){cursor=frame.first;return frame.second;}
  return {};
 }
};
}
