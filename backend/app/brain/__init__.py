"""The dialogue "brain": deterministic rules, no LLM.

text.py       normalisation, number words, yes/no
entities.py   dates, times, names, phones, services, providers, choices
intents.py    weighted-pattern intent scoring with confidence
retrieval.py  BM25 over the FAQ (keyword retrieval, returns pre-written answers)
dialog.py     slot-filling state machine that ties everything together
"""
