#include "core.hpp"
#include <stdexcept>
#include <memory>
int main(){
  auto require=[](bool value){if(!value)throw std::runtime_error("core assertion failed");};
  auto s=ajin::convert(16384,4000,160);
  require(s.angle_mdeg==90000 && s.distance_mm==1000 && s.quality==40);
  auto raw=ajin::convert_v2(16384,4000,252);
  require(raw.angle_mdeg==90000 && raw.distance_mm==1000 && raw.quality==252);
  ajin::LatestTwo<int> ring;
  ring.push(std::make_shared<int>(1));ring.push(std::make_shared<int>(2));ring.push(std::make_shared<int>(3));
  uint64_t cursor=0,lost=0;
  require(*ring.next(cursor,lost)==2 && lost==1);
  require(*ring.next(cursor,lost)==3 && lost==1);
  require(!ring.next(cursor,lost));
}
