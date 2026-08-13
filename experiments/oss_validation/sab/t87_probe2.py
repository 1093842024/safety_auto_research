import numpy as np
import csv
import cftime
import iris

import os
os.chdir("/Users/glennge/work/github/AI_research/safety_auto_research/experiments/oss_validation/sab/task_87_polynomial_fit")
fname = "benchmark/datasets/polynomial_fit/A1B_north_america.nc"
cube = iris.load_cube(fname)
location = next(cube.slices(["time"]))
x_points = location.coord("time").points
y_points = location.data
print("x dtype", x_points.dtype, "y dtype", y_points.dtype, "n", len(x_points))
degree = 2
p = np.polyfit(x_points, y_points, degree)
y_fitted = np.polyval(p, x_points)
print("first fitted:", repr(y_fitted[0]))
print("gold first:   296.78149359990067")
print("match:", repr(y_fitted[0]) == "296.78149359990067")
