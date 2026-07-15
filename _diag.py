import socket
s = socket.socket()
s.settimeout(3)
r = s.connect_ex(("127.0.0.1", 13307))
s.close()
print("tunnel OK" if r == 0 else f"tunnel DOWN ({r})")

s = socket.socket()
s.settimeout(3)
r = s.connect_ex(("127.0.0.1", 8000))
s.close()
print("backend OK" if r == 0 else f"backend DOWN ({r})")

s = socket.socket()
s.settimeout(3)
r = s.connect_ex(("127.0.0.1", 5173))
s.close()
print("frontend OK" if r == 0 else f"frontend DOWN ({r})")
