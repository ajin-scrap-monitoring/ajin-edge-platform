#include "core.hpp"
#include "sl_lidar.h"
#include "sl_lidar_driver.h"
#include "lidar.grpc.pb.h"
#include <grpcpp/grpcpp.h>
#include <nlohmann/json.hpp>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <cmath>
#include <map>
#include <vector>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <random>
#include <regex>
#include <sstream>
#include <thread>
#include <unistd.h>
#include <sys/stat.h>
using namespace std::chrono;
namespace wire=ajin::edge::lidar::v1;
using json=nlohmann::json;
std::atomic<bool> stopping{false};
int64_t monotonic_ns(){return duration_cast<nanoseconds>(steady_clock::now().time_since_epoch()).count();}
int64_t unix_ms(){return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();}
std::string iso(int64_t ms){std::time_t seconds=ms/1000;std::tm tm{};gmtime_r(&seconds,&tm);std::ostringstream out;out<<std::put_time(&tm,"%Y-%m-%dT%H:%M:%S")<<'.'<<std::setw(3)<<std::setfill('0')<<ms%1000<<'Z';return out.str();}
void signal_stop(int){stopping=true;}
void check(sl_result result,const char* action){if(!SL_IS_OK(result))throw std::runtime_error(std::string(action)+":"+std::to_string(result));}
struct Service:wire::LidarScanSource::Service {
 ajin::LatestTwo<wire::ScanFrame> ring; std::atomic<uint64_t> lost{0};std::atomic<int> subscribers{0};
 grpc::Status SubscribeScans(grpc::ServerContext* context,const wire::SubscribeRequest* request,grpc::ServerWriter<wire::ScanFrame>* writer) override {
  if(request->consumer_id().empty()||request->consumer_id().size()>128)return {grpc::StatusCode::INVALID_ARGUMENT,"consumer_id required"};
  if(subscribers.fetch_add(1)>=8){--subscribers;return {grpc::StatusCode::RESOURCE_EXHAUSTED,"subscriber limit"};}
  struct Guard {std::atomic<int>& n;~Guard(){--n;}} guard{subscribers};
  uint64_t cursor=0,skipped=0;
  while(!stopping&&!context->IsCancelled()){
   auto before=skipped;auto frame=ring.next(cursor,skipped);lost+=skipped-before;
   if(frame){if(!writer->Write(*frame))break;}else std::this_thread::sleep_for(milliseconds(20));
  }
  return grpc::Status::OK;
 }
};
int main(int argc,char** argv){
 try{
  std::map<std::string,std::string> options;
  for(int i=1;i<argc;i+=2){if(i+1==argc)throw std::runtime_error("option requires value");std::string key=argv[i];if(!std::regex_match(key,std::regex("--(sensor-id|edge-id|config-revision|ip|port|endpoint|status-dir|schema-version)"))||options.count(key))throw std::runtime_error("unknown/duplicate option");options[key]=argv[i+1];}
  auto required=[&](const std::string& key){if(!options.count(key)||options[key].empty())throw std::runtime_error("missing "+key);return options[key];};
  auto sensor=required("--sensor-id"),edge=required("--edge-id"),revision=required("--config-revision"),ip=required("--ip"),endpoint=required("--endpoint"),status_dir=required("--status-dir");
  auto service_name="lidar-driver-"+(sensor.rfind("lidar-",0)==0?sensor.substr(6):sensor);
  auto deployment_identity = [](const char* name) {
    const char* value = std::getenv(name);
    if (!value || !std::regex_match(value, std::regex("[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"))) {
      throw std::runtime_error(std::string("invalid deployment metadata: ") + name);
    }
    return std::string(value);
  };
  const auto site_id = deployment_identity("SITE_ID");
  const auto deployment_revision = deployment_identity("DEPLOYMENT_REVISION");
  for(auto value:{sensor,edge,revision})if(!std::regex_match(value,std::regex("[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")))throw std::runtime_error("unsafe identity");
  if(endpoint.rfind("unix:/",0)!=0||endpoint.size()>100)throw std::runtime_error("production endpoint must be UDS");
  int port=8089;if(options.count("--port")){size_t end=0;port=std::stoi(options["--port"],&end);if(end!=options["--port"].size())throw std::runtime_error("invalid port");}if(port<1||port>65535)throw std::runtime_error("invalid port");
  std::ifstream random("/proc/sys/kernel/random/uuid");std::string instance;random>>instance;if(instance.empty())throw std::runtime_error("instance UUID unavailable");
  const auto schema=options.count("--schema-version")?options["--schema-version"]:"1.0";
  if(schema!="1.0"&&schema!="2.0")throw std::runtime_error("unsupported schema version");
  std::ifstream boot_file("/proc/sys/kernel/random/boot_id");std::string boot_id;boot_file>>boot_id;
  if(schema=="2.0"&&boot_id.empty())throw std::runtime_error("boot clock domain unavailable");
  std::filesystem::create_directories(status_dir);std::filesystem::create_directories(std::filesystem::path(endpoint.substr(5)).parent_path());
  std::signal(SIGTERM,signal_stop);std::signal(SIGINT,signal_stop);umask(0007);
  Service service;grpc::ServerBuilder builder;builder.AddListeningPort(endpoint,grpc::InsecureServerCredentials());builder.RegisterService(&service);builder.SetMaxSendMessageSize(4*1024*1024);
  auto server=builder.BuildAndStart();if(!server)throw std::runtime_error("UDS bind failed");
  std::atomic<int64_t> progress{monotonic_ns()},last_scan{0};std::atomic<uint64_t> sequence{0},errors{0};std::atomic<int> state{0};std::atomic<bool> watchdog_done{false};
  auto started=iso(unix_ms());
  std::thread watchdog([&]{
   while(!watchdog_done){
    auto now=monotonic_ns();bool hung=now-progress.load()>15'000'000'000LL;
    json status={{"schema_version","1.0"},{"service","lidar-driver-"+sensor},{"edge_id",edge},{"sensor_id",sensor},{"config_revision",revision},{"instance_id",instance},{"started_at",started},{"updated_at",iso(unix_ms())},{"last_progress_at",iso(unix_ms()-(now-progress.load())/1'000'000)},{"state",hung?"FATAL":state==0?"STARTING":state==1?"HEALTHY":"RETRYING"},{"reason_codes",hung?json::array({"LOOP_HANG"}):state==2?json::array({"SDK_RETRY"}):json::array()},{"sequence",sequence.load()},{"frame_loss",service.lost.load()},{"sdk_errors",errors.load()},{"last_scan_unix_ms",last_scan.load()}};
    status["reported_at"]=iso(unix_ms());status["service_version"]="1.0.0";status["service"]=service_name;
    status["site_id"] = site_id;
    status["deployment_revision"] = deployment_revision;
    auto path=std::filesystem::path(status_dir)/(service_name+".json");auto temp=path.string()+"."+instance+".tmp";
    try{std::ofstream file(temp);file.exceptions(std::ios::badbit|std::ios::failbit);file<<status.dump()<<'\n';file.close();std::filesystem::rename(temp,path);}catch(const std::exception& e){std::cerr<<"status write: "<<e.what()<<'\n';}
    if(hung)std::_Exit(70);
    for(int i=0;i<20&&!watchdog_done;++i)std::this_thread::sleep_for(milliseconds(100));
   }
  });
  unsigned delay=1;
  while(!stopping){
   progress=monotonic_ns();
   try{
    auto channel_result=sl::createUdpChannel(ip,port);if(!channel_result)throw std::runtime_error("create UDP channel");std::unique_ptr<sl::IChannel> channel(*channel_result);
    auto driver_result=sl::createLidarDriver();if(!driver_result)throw std::runtime_error("create driver");std::unique_ptr<sl::ILidarDriver> driver(*driver_result);
    check(driver->connect(channel.get()),"connect");progress=monotonic_ns();
    sl_lidar_response_device_info_t info{};sl_lidar_response_device_health_t health{};
    check(driver->getDeviceInfo(info,1000),"device info");check(driver->getHealth(health,1000),"health");if(health.status==SL_LIDAR_STATUS_ERROR)throw std::runtime_error("device unhealthy");
    check(driver->startScan(false,true),"start scan");auto stable=steady_clock::now();int64_t previous=0;
    while(!stopping){
     std::vector<sl_lidar_response_measurement_node_hq_t> nodes(32768);size_t count=nodes.size();
     auto result=driver->grabScanDataHq(nodes.data(),count,1000);
     // Timestamp completed scan receipt before validation, sorting or coordinate processing.
     const auto stamp=monotonic_ns();const auto acquired_utc=unix_ms();progress=stamp;check(result,"grab scan");
     if(count==0||count>nodes.size())throw std::runtime_error("invalid HQ count");
     // v2 preserves SDK point order; legacy consumers retain the sorted v1 behavior.
     if(schema=="1.0")check(driver->ascendScanData(nodes.data(),count),"ascend scan");
     if(!previous&&schema=="1.0"){previous=stamp;continue;}
     double hz=previous?1e9/double(stamp-previous):0;previous=stamp;
     if(!std::isfinite(hz)||hz<0)throw std::runtime_error("invalid scan rate");
     auto frame=std::make_shared<wire::ScanFrame>();frame->set_schema_version(schema);frame->set_edge_id(edge);frame->set_sensor_id(sensor);frame->set_sequence(++sequence);frame->set_acquired_at_unix_ms(acquired_utc);frame->set_acquired_monotonic_ns(stamp);frame->set_sdk_status("OK");frame->set_scan_hz(hz);frame->set_instance_id(instance);frame->set_config_revision(revision);
     frame->set_scan_id(instance+":"+std::to_string(frame->sequence()));frame->set_clock_domain_id(boot_id);
     for(size_t i=0;i<count;++i){auto value=schema=="2.0"?ajin::convert_v2(nodes[i].angle_z_q14,nodes[i].dist_mm_q2,nodes[i].quality):ajin::convert(nodes[i].angle_z_q14,nodes[i].dist_mm_q2,nodes[i].quality);auto sample=frame->add_samples();sample->set_angle_mdeg(value.angle_mdeg);sample->set_distance_mm(value.distance_mm);sample->set_quality(value.quality);sample->set_sdk_invalid_range(nodes[i].dist_mm_q2==0);}
     service.ring.push(frame);last_scan=frame->acquired_at_unix_ms();state=1;progress=stamp;
     if(steady_clock::now()-stable>=seconds(30))delay=1;
    }
    driver->stop(1000);driver->disconnect();
   }catch(const std::exception& e){++errors;state=2;std::cerr<<sensor<<": "<<e.what()<<'\n';}
   for(unsigned i=0;i<delay*10&&!stopping;++i){progress=monotonic_ns();std::this_thread::sleep_for(milliseconds(100));}delay=std::min(30u,delay*2);
  }
  server->Shutdown(system_clock::now()+seconds(2));watchdog_done=true;watchdog.join();return 0;
 }catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 2;}
}
