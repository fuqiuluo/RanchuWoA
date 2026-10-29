"""serial_adb_gateway v3 — 串口连接永不断开。

QEMU Windows chardev socket 的缺陷：客户端断开后 server 不再接受连接。
对策：启动即建立串口长连接，永不主动断开；adb 侧每个新连接直接桥接到
这条常驻连接。guest 侧 b.sh 的 while 循环保证 nc 侧自愈。
"""
import socket
import threading

SERIAL_PORT = 4446
ADB_PORT = 5559

serial_sock = None
serial_lock = threading.Lock()
adb_sock = None
adb_lock = threading.Lock()


def serial_reader():
    global adb_sock
    while True:
        try:
            data = serial_sock.recv(65536)
        except OSError:
            print("[gw] 串口读失败（不应发生）")
            return
        if not data:
            print("[gw] 串口对端关闭")
            return
        with adb_lock:
            a = adb_sock
        if a:
            try:
                a.sendall(data)
            except OSError:
                pass


def adb_writer(a):
    global adb_sock
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            with serial_lock:
                serial_sock.sendall(data)
    except OSError:
        pass
    finally:
        with adb_lock:
            if adb_sock is a:
                adb_sock = None
        try:
            a.close()
        except OSError:
            pass
        print("[gw] adb 侧断开（串口保持）")


def main():
    global serial_sock, adb_sock
    serial_sock = socket.create_connection(("127.0.0.1", SERIAL_PORT), timeout=10)
    print(f"[gw] 串口长连接建立 :{SERIAL_PORT}")
    threading.Thread(target=serial_reader, daemon=True).start()

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", ADB_PORT))
    srv.listen(4)
    print(f"[gw] adb 监听 :{ADB_PORT}")
    while True:
        c, _ = srv.accept()
        with adb_lock:
            old = adb_sock
            adb_sock = c
        if old:
            try:
                old.close()
            except OSError:
                pass
        print("[gw] adb 侧连入")
        threading.Thread(target=adb_writer, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
