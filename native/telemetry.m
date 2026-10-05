// Read-only Apple SMC layout and M5 sensor mappings adapted from exelban/Stats.
// See THIRD_PARTY_NOTICES.md. This helper implements no SMC writes.
#import <Foundation/Foundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/ps/IOPowerSources.h>
#include <mach/mach.h>
#include <sys/sysctl.h>
#include <unistd.h>
#include <math.h>

typedef struct { uint8_t major,minor,build,reserved; uint16_t release; } SMCVersion;
typedef struct { uint16_t version,length; uint32_t cpu,gpu,mem; } SMCLimit;
typedef struct { uint32_t dataSize,dataType; uint8_t attributes; } SMCInfo;
typedef struct { uint32_t key; SMCVersion vers; SMCLimit limit; SMCInfo info; uint8_t result,status,command; uint32_t data; uint8_t bytes[32]; } SMCData;
_Static_assert(sizeof(SMCData)==80, "SMC ABI must be 80 bytes");
_Static_assert(offsetof(SMCData, bytes)==48, "SMC byte offset must be 48");
static io_connect_t smc;
static uint32_t keycode(const char *k){return (uint32_t)(unsigned char)k[0]<<24|(uint32_t)(unsigned char)k[1]<<16|(uint32_t)(unsigned char)k[2]<<8|(unsigned char)k[3];}
static double sensor(const char *key){
 if(!smc)return NAN;
 SMCData in={0},out={0};size_t n=sizeof(out);in.key=keycode(key);in.command=9;
 if(IOConnectCallStructMethod(smc,2,&in,sizeof(in),&out,&n)||out.result||out.info.dataSize>32)return NAN;
 uint32_t type=out.info.dataType;in.info.dataSize=out.info.dataSize;in.command=5;n=sizeof(out);
 if(IOConnectCallStructMethod(smc,2,&in,sizeof(in),&out,&n)||out.result)return NAN;
 if(type==keycode("flt ")){float v;memcpy(&v,out.bytes,4);return v;}
 if(type==keycode("sp78"))return (int16_t)((out.bytes[0]<<8)|out.bytes[1])/256.0;
 if(type==keycode("fpe2"))return ((out.bytes[0]<<8)|out.bytes[1])/4.0;
 if(type==keycode("ui8 "))return out.bytes[0];
 if(type==keycode("ui16"))return (out.bytes[0]<<8)|out.bytes[1];
 return NAN;
}
// BEGIN systemPower
static double systemPower(void){
 // Some desktops expose PSTR but return zero. Try total DC input, then
 // the DC input rail; these are internal readings, not calibrated wall draw.
 const char *keys[]={"PSTR","PDTR","PD0R"};
 for(int i=0;i<3;i++){
  double watts=sensor(keys[i]);
  if(isfinite(watts)&&watts>0&&watts<=1000)return watts;
 }
 return NAN;
}
// END systemPower
static double average(const char **keys,int count){double total=0;int found=0;for(int i=0;i<count;i++){double v=sensor(keys[i]);if(isfinite(v)&&v>0&&v<130){total+=v;found++;}}return found?total/found:NAN;}
static id number(double n){return isfinite(n)?@(n):(id)[NSNull null];}
static NSString *powerSource(){
 CFTypeRef info=IOPSCopyPowerSourcesInfo();if(!info)return @"Unknown";
 CFStringRef type=IOPSGetProvidingPowerSourceType(info);
 NSString *result=type?[(__bridge NSString *)type copy]:@"Unknown";CFRelease(info);return result;
}
static double gpuUsage(){
 io_iterator_t it=0;if(IOServiceGetMatchingServices(kIOMainPortDefault,IOServiceMatching("IOAccelerator"),&it))return NAN;
 io_object_t obj;double value=NAN;while((obj=IOIteratorNext(it))){CFTypeRef stats=IORegistryEntryCreateCFProperty(obj,CFSTR("PerformanceStatistics"),kCFAllocatorDefault,0);if(stats&&CFGetTypeID(stats)==CFDictionaryGetTypeID()){CFNumberRef v=CFDictionaryGetValue(stats,CFSTR("Device Utilization %"));if(v&&CFGetTypeID(v)==CFNumberGetTypeID())CFNumberGetValue(v,kCFNumberDoubleType,&value);}if(stats)CFRelease(stats);IOObjectRelease(obj);}IOObjectRelease(it);return value;
}
static NSArray *gpuProcesses(){
 static NSMutableDictionary *previous; static double last=0;
 double now=NSProcessInfo.processInfo.systemUptime;
 NSMutableDictionary *current=[NSMutableDictionary dictionary],*totals=[NSMutableDictionary dictionary];
 io_iterator_t roots=0;IOServiceGetMatchingServices(kIOMainPortDefault,IOServiceMatching("IOAccelerator"),&roots);io_object_t root;
 while((root=IOIteratorNext(roots))){io_iterator_t it=0;IORegistryEntryCreateIterator(root,kIOServicePlane,kIORegistryIterateRecursively,&it);io_object_t child;
  while((child=IOIteratorNext(it))){if(IOObjectConformsTo(child,"AGXDeviceUserClient")){
   NSString *who=CFBridgingRelease(IORegistryEntryCreateCFProperty(child,CFSTR("IOUserClientCreator"),kCFAllocatorDefault,0));
   NSArray *uses=CFBridgingRelease(IORegistryEntryCreateCFProperty(child,CFSTR("AppUsage"),kCFAllocatorDefault,0));uint64_t rid=0;IORegistryEntryGetRegistryEntryID(child,&rid);
   if([who isKindOfClass:NSString.class]&&[uses isKindOfClass:NSArray.class]&&uses.count){int pid=0;sscanf(who.UTF8String,"pid %d,",&pid);double total=0;BOOL valid=NO;
    for(NSDictionary *u in uses){id n=u[@"accumulatedGPUTime"];if([n isKindOfClass:NSNumber.class]){total+=[n doubleValue];valid=YES;}}
    if(pid>0&&valid){NSString *key=[NSString stringWithFormat:@"%llu",rid];current[key]=@(total);NSString *pidkey=[NSString stringWithFormat:@"%d",pid];
     NSMutableDictionary *row=totals[pidkey];if(!row){row=[@{@"pid":@(pid),@"name":[who componentsSeparatedByString:@", "].lastObject?:@"Process",@"gpu":@0,@"known":@NO} mutableCopy];totals[pidkey]=row;}
     NSNumber *old=previous[key];if(old&&total>=old.doubleValue&&now>last&&now-last<5){double pct=(total-old.doubleValue)/1e9/(now-last)*100;row[@"gpu"]=@([row[@"gpu"] doubleValue]+pct);row[@"known"]=@YES;}
    }
   }
  }IOObjectRelease(child);}IOObjectRelease(it);IOObjectRelease(root);
 }IOObjectRelease(roots);previous=current;last=now;
 NSMutableArray *rows=[NSMutableArray array];for(NSMutableDictionary *r in totals.allValues){if(![r[@"known"] boolValue])r[@"gpu"]=[NSNull null];[r removeObjectForKey:@"known"];[rows addObject:r];}return rows;
}
int main(int argc,const char **argv){
 @autoreleasepool {
  io_service_t service=IOServiceGetMatchingService(kIOMainPortDefault,IOServiceMatching("AppleSMC"));if(service){IOServiceOpen(service,mach_task_self(),0,&smc);IOObjectRelease(service);}
  char chip[128]="Apple Silicon";size_t size=sizeof(chip);sysctlbyname("machdep.cpu.brand_string",chip,&size,NULL,0);
  uint64_t memory=0;size=sizeof(memory);sysctlbyname("hw.memsize",&memory,&size,NULL,0);
  const char *cpuKeys[]={"Tp00","Tp04","Tp08","Tp0C","Tp0G","Tp0K","Tp0O","Tp0R","Tp0U","Tp0X","Tp0a","Tp0d","Tp0g","Tp0j","Tp0m","Tp0p","Tp0u","Tp0y"};
  const char *gpuKeys[]={"Tg0U","Tg0X","Tg0d","Tg0g","Tg0j","Tg1Y","Tg1c","Tg1g"};
  host_cpu_load_info_data_t prev={0};BOOL initialized=NO;
  do {@autoreleasepool {
   host_cpu_load_info_data_t ticks;mach_msg_type_number_t count=HOST_CPU_LOAD_INFO_COUNT;double cpu=NAN;
   if(host_statistics(mach_host_self(),HOST_CPU_LOAD_INFO,(host_info_t)&ticks,&count)==KERN_SUCCESS){if(initialized){uint64_t total=0,idle=0;for(int i=0;i<CPU_STATE_MAX;i++){uint32_t delta=ticks.cpu_ticks[i]-prev.cpu_ticks[i];total+=delta;if(i==CPU_STATE_IDLE)idle=delta;}if(total)cpu=100.0*(total-idle)/total;}prev=ticks;initialized=YES;}
   vm_statistics64_data_t vm;count=HOST_VM_INFO64_COUNT;vm_size_t page;host_page_size(mach_host_self(),&page);double used=NAN,compressed=NAN,available=NAN,cached=NAN,purgeable=NAN;
   if(host_statistics64(mach_host_self(),HOST_VM_INFO64,(host_info64_t)&vm,&count)==KERN_SUCCESS){used=((double)vm.active_count+vm.inactive_count+vm.wire_count+vm.compressor_page_count-vm.purgeable_count-vm.external_page_count)*page/1073741824.0;compressed=(double)vm.compressor_page_count*page/1073741824.0;available=((double)vm.free_count+vm.inactive_count)*page/1073741824.0;cached=(double)vm.external_page_count*page/1073741824.0;purgeable=(double)vm.purgeable_count*page/1073741824.0;}
   struct xsw_usage swap;size=sizeof(swap);double swapGB=NAN;if(!sysctlbyname("vm.swapusage",&swap,&size,NULL,0))swapGB=swap.xsu_used/1073741824.0;
   NSArray *thermal=@[@"Nominal",@"Fair",@"Serious",@"Critical"];NSInteger state=[NSProcessInfo processInfo].thermalState;
   NSDictionary *data=@{@"gpuProcesses":gpuProcesses(),@"chip":@(chip),@"cpuPercent":number(cpu),@"gpuPercent":number(gpuUsage()),@"memoryUsedGB":number(used),@"memoryAvailableGB":number(available),@"cachedFilesGB":number(cached),@"purgeableGB":number(purgeable),@"memoryTotalGB":number(memory/1073741824.0),@"compressedGB":number(compressed),@"swapGB":number(swapGB),@"cpuTemp":number(average(cpuKeys,18)),@"gpuTemp":number(average(gpuKeys,8)),@"fanRPM":@[number(sensor("F0Ac")),number(sensor("F1Ac"))],@"thermal":state<4?thermal[state]:@"Unknown",@"at":@([[NSDate date] timeIntervalSince1970])};
   NSMutableDictionary *withPower=[data mutableCopy];
   withPower[@"systemWatts"]=number(systemPower());
   withPower[@"powerSource"]=powerSource();
   NSData *json=[NSJSONSerialization dataWithJSONObject:withPower options:0 error:NULL];fwrite(json.bytes,1,json.length,stdout);fputc('\n',stdout);fflush(stdout);
  }if(argc>1)break;sleep(1);}while(getppid()!=1);
  if(smc)IOServiceClose(smc);
 }
 return 0;
}
