#!/usr/bin/env python3
"""Debug riskfolio sqrtm issue."""
import riskfolio as rp
import numpy as np
import pandas as pd

np.random.seed(42)
returns = pd.DataFrame(np.random.randn(504, 3) * 0.02, columns=['A', 'B', 'C'])

port = rp.Portfolio(returns=returns)
print("mu:", port.mu)
print("cov:", port.cov)
print("cov shape:", port.cov.shape)
print("cov values:\n", port.cov.values)

# Test EqualWeight
print("\n=== EqualWeight ===")
w = port.optimization(model='EqualWeight', hist=True)
print("Weights:", w['weights'].values)
print("Sum:", w['weights'].sum())
