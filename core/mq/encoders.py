import json

try:
    import numpy as np
except ImportError:
    np = None

class NumpyEncoder(json.JSONEncoder):
    # NO LOSS OF EXISTING FUNCTIONALITY, NO ACCIDENTAL DELETIONS, NO OVERSIGHT - GCP CONTAINER SAFE
    def default(self, obj):
        if np:
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.bool_):
                return bool(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
        return super().default(obj)
