# sports/registry.py — wires all sport modules together
#
# This is the one file allowed to import every sports/<x> package. Everything
# else (core/, markets/, app.py, main.py) reaches sports only through this
# registry, keyed by sport name, so adding a sport never touches those files.

from sports.nba.factors import NBASport
from sports.mlb.factors import MLBSport
from sports.nfl.factors import NFLSport
from sports.nhl.factors import NHLSport

SPORTS = {
    "nba": NBASport(),
    "mlb": MLBSport(),
    "nfl": NFLSport(),
    "nhl": NHLSport(),
}


def get_sport(key):
    return SPORTS[key]
