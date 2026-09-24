"""CoinDCX TOP-20 crypto futures SIGNAL-ONLY intelligence bot.

Safety contract (asserted at boot by `app.safety.assert_signal_only`):
  * no exchange trading key is ever read, requested, or stored;
  * no module in this package can place, cancel, modify or close anything;
  * every failure path resolves to NO TRADE (fail-closed).
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
