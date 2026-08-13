import os, numpy as np, iris
os.chdir("/Users/glennge/work/github/AI_research/safety_auto_research/experiments/oss_validation/sab/task_87_polynomial_fit")
cube = iris.load_cube("benchmark/datasets/polynomial_fit/A1B_north_america.nc")
location = next(cube.slices(["time"]))
x = np.asarray(location.coord("time").points, dtype=np.float64)
y = np.asarray(location.data, dtype=np.float32)  # float32
np.save("/tmp/t87_x.npy", x)
np.save("/tmp/t87_y.npy", y)
print("saved x dtype", x.dtype, "y dtype", y.dtype, "n", len(x))

# Now try polyfit under THIS numpy to record baseline
p = np.polyfit(x, y, 2)
yf = np.polyval(p, x)
print("this-numpy first fitted:", repr(yf[0]))
print("gold first:              296.78149359990067")
