"""guest_file_server.py — 本机文件下发服务（供 guest 侧 nc 拉取）。
用法: python guest_file_server.py PORT:FILE [PORT:FILE ...]
每次连接把整个文件发过去然后关闭，可重复接受连接。
"""
import socket
import sys
import threading

SERVES = [(int(a.split(":")[0]), a.split(":", 1)[1]) for a in sys.argv[1:]]


def serve(port, path):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    print(f"serving :{port} <- {path}", flush=True)
    while True:
        c, _ = srv.accept()
        try:
            with open(path, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    c.sendall(b)
        except OSError:
            pass
        finally:
            c.close()


for p, f in SERVES:
    threading.Thread(target=serve, args=(p, f), daemon=True).start()
threading.Event().wait()
