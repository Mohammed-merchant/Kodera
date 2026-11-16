import importlib.metadata as m

for d in m.distributions():
    try:
        d.read_text("top_level.txt")
    except OSError as e:
        print("PROBLEM:", d._path)
        print("ERROR:", repr(e))