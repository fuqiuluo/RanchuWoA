"""adb 反向中继：
  listen 5557 <- guest 主动连入（数据管道端）
  listen 5558 <- adb server 连入（adb connect 127.0.0.1:5558）
两侧一对接就双向直通。可多会话（每对独立）。
"""
import socket
import threading

GUEST_PORT = 5557   # guest 连这里
ADB_PORT = 5558     # adb connect 连这里

pipe_sock = None
pipe_lock = threading.Lock()
pipe_ready = threading.Event()


def pump(a, b, tag):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        try:
            a.close()
        except OSError:
            pass
        try:
            b.close()
        except OSError:
            pass
        print(f"[relay] {tag} 断开")


def guest_side():
    global pipe_sock
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", GUEST_PORT))
    srv.listen(4)
    print(f"[relay] 等待 guest 连入 127.0.0.1:{GUEST_PORT}")
    while True:
        c, _ = srv.accept()
        with pipe_lock:
            pipe_sock = c
        pipe_ready.set()
        print("[relay] guest 管道已连接")


def adb_side():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", ADB_PORT))
    srv.listen(4)
    print(f"[relay] adb 端监听 127.0.0.1:{ADB_PORT}")
    while True:
        c, _ = srv.accept()
        print("[relay] adb 侧连接进入，等待 guest 管道...")
        if not pipe_ready.wait(30):
            print("[relay] 无 guest 管道，断开")
            c.close()
            continue
        with pipe_lock:
            g = pipe_sock
            pipe_sock = None
        pipe_ready.clear()
        print("[relay] 对接成功，开始双向直通")
        t1 = threading.Thread(target=pump, args=(c, g, "adb→guest"), daemon=True)
        t2 = threading.Thread(target=pump, args=(g, c, "guest→adb"), daemon=True)
        t1.start()
        t2.start()


if __name__ == "__main__":
    threading.Thread(target=guest_side, daemon=True).start()
    adb_side()
