import hypothesis.strategies as st
from hypothesis import given
@given(st.integers(min_value=0, max_value=100))
def test_exposure(x): assert x >= 0
