#include "esp_partition.h"
#include "watcher_storage.h"
#include <assert.h>
#include <string.h>

#define BASE 0x1fbf000u
#define LENGTH 0x41000u
static esp_flash_t flash={0x2000000}, foreign_flash={0x2000000};
esp_flash_t *esp_flash_default_chip=&flash;
static esp_partition_t partitions[8];
static unsigned count, writes;
static bool read_error;
static unsigned char data[LENGTH];
struct iterator { unsigned index; };
static struct iterator iterator;

static void add(const char *name, unsigned address, unsigned size) {
    esp_partition_t *p=&partitions[count++];
    p->flash_chip=&flash; p->address=address; p->size=size;
    strcpy(p->label,name);
}
static void setup(void) {
    count=0; writes=0; read_error=false; flash.size=0x2000000;
    add("ota_0",0x20000,0x600000);
    add("agentcfg",0x12000,0xe000);
    add("storage",0xd20000,0x400000);
    memset(data,255,sizeof(data));
}
const esp_partition_t *esp_partition_find_first(esp_partition_type_t t,esp_partition_subtype_t s,const char *name) {
    (void)t;(void)s;
    for (unsigned i=0;i<count;++i) if (!strcmp(name,partitions[i].label)) return &partitions[i];
    return NULL;
}
esp_partition_iterator_t esp_partition_find(esp_partition_type_t t,esp_partition_subtype_t s,const char *name) {
    (void)t;(void)s;(void)name; iterator.index=0; return count ? &iterator : NULL;
}
const esp_partition_t *esp_partition_get(esp_partition_iterator_t it) {return &partitions[it->index];}
esp_partition_iterator_t esp_partition_next(esp_partition_iterator_t it) {return ++it->index<count ? it : NULL;}
void esp_partition_iterator_release(esp_partition_iterator_t it) {(void)it;}
esp_err_t esp_flash_get_size(esp_flash_t *f,uint32_t *out) {*out=f->size;return 0;}
esp_err_t esp_partition_register_external(esp_flash_t *f,size_t offset,size_t size,const char *label,
    esp_partition_type_t t,esp_partition_subtype_t s,const esp_partition_t **out) {
    (void)t;(void)s;
    assert(f==&flash); assert(offset>=BASE && offset+size<=BASE+LENGTH);
    add(label,offset,size); *out=&partitions[count-1]; return 0;
}
esp_err_t esp_partition_deregister_external(const esp_partition_t *p) {
    unsigned i=(unsigned)(p-partitions); assert(i>=3 && i<count);
    memmove(&partitions[i],&partitions[i+1],(--count-i)*sizeof(*p)); return 0;
}
esp_err_t esp_partition_read(const esp_partition_t *p,size_t offset,void *out,size_t size) {
    assert(p->address>=BASE && offset+size<=p->size);
    if (read_error) return ESP_ERR_INVALID_STATE;
    memcpy(out,&data[p->address-BASE+offset],size); return 0;
}
esp_err_t esp_partition_write(const esp_partition_t *p,size_t offset,const void *in,size_t size) {
    /* No code under test may write any factory partition or erase anything. */
    assert(p->address==BASE && offset==0 && size==32);
    assert(writes==0); memcpy(data,in,size); ++writes; return 0;
}
int main(int argc,char **argv) {
    assert(argc==2); setup();
    const char *test=argv[1];
    if (!strcmp(test,"success")) {
        assert(desk_watcher_storage_init()==0 && writes==1 && count==5);
        for (unsigned i=0x1000;i<LENGTH;++i) assert(data[i]==255);
        data[0x1100]=0x42;
        count=3; writes=0; /* reboot: keep flash, remove RAM registrations */
        assert(desk_watcher_storage_init()==0 && writes==0 && data[0x1100]==0x42);
        assert(desk_watcher_storage_init()==0 && writes==0);
        return 0;
    }
    if (!strcmp(test,"unknown")) data[LENGTH-1]=0x42;
    else if (!strcmp(test,"metadata")) data[0]='D';
    else if (!strcmp(test,"overlap")) add("future",BASE-0x1000,0x2000);
    else if (!strcmp(test,"capacity")) flash.size=0x1000000;
    else if (!strcmp(test,"layout")) partitions[0].address+=0x1000;
    else if (!strcmp(test,"read_error")) read_error=true;
    else if (!strcmp(test,"foreign")) {add("tuya",0x1fc0000,0x40000);partitions[3].flash_chip=&foreign_flash;}
    else if (!strcmp(test,"unmarked")) add("tuya",0x1fc0000,0x40000);
    else assert(false);
    unsigned original=count;
    unsigned char before[LENGTH]; memcpy(before,data,LENGTH);
    assert(desk_watcher_storage_init()!=0);
    assert(writes==0 && count==original && memcmp(before,data,LENGTH)==0);
    return 0;
}
