import numpy as np
x = np.load("/tmp/t87_x.npy")
y = np.load("/tmp/t87_y.npy")
p = np.polyfit(x, y, 2)
yf = np.polyval(p, x)
first = repr(yf[0])
gold = "296.78149359990067"
print(f"numpy {np.__version__}: {first}  match={first==gold}")
