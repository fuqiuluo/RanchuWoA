#!/bin/busybox sh
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
/bin/busybox --install -s /bin >/dev/null 2>&1
mount -t proc proc /proc >/dev/null 2>&1
mount -t sysfs sys /sys >/dev/null 2>&1
mount -t devtmpfs none /dev >/dev/null 2>&1
sleep 2
VDSK=6619136
VDC=212832
DBG=/e2/usr/sbin/debugfs
export LD_LIBRARY_PATH=/e2/usr/lib
echo "=== nullgfx inject: dd out vendor ==="
dd if=/dev/vda of=/tmp/vendor.ext4 bs=512 skip=$VDSK count=$VDC status=none || { echo DDOUT_FAIL; exec /bin/busybox sh; }
echo "=== debugfs write ==="
"$DBG" -w -R "write /inj/mapper.nullgfx.so lib64/hw/mapper.nullgfx.so" /tmp/vendor.ext4
"$DBG" -w -R "write /inj/nullgfx.rc etc/init/nullgfx.rc" /tmp/vendor.ext4
"$DBG" -w -R "write /inj/nullgfx.xml etc/vintf/manifest/nullgfx.xml" /tmp/vendor.ext4
"$DBG" -w -R "set_inode_field lib64/hw/mapper.nullgfx.so mode 0100644" /tmp/vendor.ext4
"$DBG" -w -R "set_inode_field etc/init/nullgfx.rc mode 0100644" /tmp/vendor.ext4
"$DBG" -w -R "set_inode_field etc/vintf/manifest/nullgfx.xml mode 0100644" /tmp/vendor.ext4
echo "=== verify ==="
"$DBG" -R "ls -l lib64/hw" /tmp/vendor.ext4 | grep nullgfx
"$DBG" -R "ls -l etc/init" /tmp/vendor.ext4 | grep nullgfx
"$DBG" -R "ls -l etc/vintf/manifest" /tmp/vendor.ext4 | grep nullgfx
echo "=== dd back ==="
dd if=/tmp/vendor.ext4 of=/dev/vda bs=512 skip=$VDSK seek=$VDSK count=$VDC conv=notrunc status=none && echo INJECT_DONE
sync
poweroff -f
exec /bin/busybox sh
