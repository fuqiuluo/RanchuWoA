/* 链接桩：仅满足链接期符号，运行时由系统 libbinder_ndk.so 真实现接管 */
void AServiceManager_addService(void* b, const char* i) { (void)b; (void)i; }
void ABinderProcess_setThreadPoolMaxThreadCount(unsigned t) { (void)t; }
void ABinderProcess_startThreadPool(void) {}
void ABinderProcess_joinThreadPool(void) {}
void* AIBinder_Class_define(const char* d, void* c, void* x, void* t) { (void)d;(void)c;(void)x;(void)t; return 0; }
void* AIBinder_Class_create(const char* d, void* c, void* t, void* x) { (void)d;(void)c;(void)t;(void)x; return 0; }
void* AIBinder_new(const void* c, void* a) { (void)c;(void)a; return 0; }
int AParcel_writeBool(void* p, unsigned char b) { (void)p;(void)b; return 0; }
int AParcel_writeInt32(void* p, int v) { (void)p;(void)v; return 0; }
int AParcel_writeInt64(void* p, long long v) { (void)p;(void)v; return 0; }
int AParcel_writeParcelFileDescriptor(void* p, int fd) { (void)p;(void)fd; return 0; }
int AParcel_writeString(void* p, const char* s, int len) { (void)p;(void)s;(void)len; return 0; }
int AParcel_marshal(const void* p, unsigned char* b, unsigned long st, unsigned long len) { (void)p;(void)b;(void)st;(void)len; return 0; }
int AParcel_getDataSize(const void* p) { (void)p; return 0; }
int AParcel_getDataPosition(const void* p) { (void)p; return 0; }
