import numpy as np
import cftime
import iris
fname = "benchmark/datasets/polynomial_fit/A1B_north_america.nc"
cube = iris.load_cube(fname)
location = next(cube.slices(["time"]))
x_points = location.coord("time").points
y_points = location.data
degree = 2
p = np.polyfit(x_points, y_points, degree)
y_fitted = np.polyval(p, x_points)
print("numpy", np.__version__)
print("y_points[0:3]", repr(y_points[0]), repr(y_points[1]), repr(y_points[2]))
print("poly coef", [repr(c) for c in p])
print("fitted[0]", repr(y_fitted[0]))
print("gold fitted[0] 296.78149359990067")
