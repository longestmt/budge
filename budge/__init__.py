"""budge — glue scripts around a stock hledger / SimpleFIN / Paisa stack.

Prime directive: no frankensteining. Everything in this package talks to the
stock components only through their stable interfaces (SimpleFIN API, CSV,
hledger journal format, hledger CLI). Each module is individually replaceable
without any change to the data.
"""

__version__ = "1.0.0"
