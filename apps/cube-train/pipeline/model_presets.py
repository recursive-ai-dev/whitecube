"""Small-model presets; safe to import in the GUI without importing torch."""
MODEL_PRESETS = {
    '1m': {'width': 160, 'parameters': 1_005_768},
    '2m': {'width': 224, 'parameters': 1_981_512},
    '4m': {'width': 320, 'parameters': 3_936_648},
    '5m': {'width': 360, 'parameters': 4_987_448},
    '8m': {'width': 456, 'parameters': 7_980_920},
    '10m': {'width': 512, 'parameters': 10_034_184},
}
DESKTOP_SIZES = ('1m', '2m', '5m', '10m')
