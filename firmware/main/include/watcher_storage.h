#ifndef DESK_WATCHER_STORAGE_H
#define DESK_WATCHER_STORAGE_H
/* Register the SDK's logical tuya partition without replacing the factory
 * partition table or writing any existing partition. Returns an ESP error. */
int desk_watcher_storage_init(void);
#endif
