import os
import re
import h5py
import numpy as np
import pandas as pd
import gradio as gr
import plotly.graph_objects as go
from larch import Group
from pathlib import Path, PurePosixPath
from scipy.signal import savgol_filter
from scipy.interpolate import PchipInterpolator
from larch.xafs import pre_edge, autobk, xftf, cauchy_wavelet
import shutil
import tempfile
import matplotlib.pyplot as plt

# --- GLOBAL INITIALIZATION ---
project_processed_path = Path("/tmp")
base_project_path = "/beamlinedata/BIOXAS-SPECTROSCOPY/projects"

# ==============================================================================
# EMISSION LINE DATABASE  (energy in eV)
# Covers K-alpha1, K-alpha2, K-beta1 and common L-lines for Z=20-50+
# Source: xraylib / NIST (rounded to nearest eV)
# ==============================================================================
EMISSION_LINES = {
    # symbol : { line_name : energy_eV }
    "Ca":  {"Ka1": 3692, "Ka2": 3688, "Kb1": 4013},
    "Ti":  {"Ka1": 4511, "Ka2": 4505, "Kb1": 4932},
    "V":   {"Ka1": 4952, "Ka2": 4945, "Kb1": 5427},
    "Cr":  {"Ka1": 5415, "Ka2": 5406, "Kb1": 5947},
    "Mn":  {"Ka1": 5899, "Ka2": 5888, "Kb1": 6490},
    "Fe":  {"Ka1": 6404, "Ka2": 6391, "Kb1": 7058},
    "Co":  {"Ka1": 6930, "Ka2": 6915, "Kb1": 7649},
    "Ni":  {"Ka1": 7478, "Ka2": 7461, "Kb1": 8265},
    "Cu":  {"Ka1": 8048, "Ka2": 8028, "Kb1": 8905},
    "Zn":  {"Ka1": 8639, "Ka2": 8616, "Kb1": 9572},
    "As":  {"Ka1":10544, "Ka2":10508, "Kb1":11726},
    "Se":  {"Ka1":11222, "Ka2":11182, "Kb1":12496},
    "Br":  {"Ka1":11924, "Ka2":11878, "Kb1":13292},
    "Kr":  {"Ka1":12649, "Ka2":12598, "Kb1":14112},
    "Sr":  {"Ka1":14165, "Ka2":14098, "Kb1":15836},
    "Mo":  {"Ka1":17479, "Ka2":17374, "Kb1":19608},
    "Pd":  {"Ka1":21177, "Ka2":21020, "Kb1":23819},
    "Ag":  {"Ka1":22163, "Ka2":21990, "Kb1":24943},
    "Cd":  {"Ka1":23174, "Ka2":22984, "Kb1":26096},
    "In":  {"Ka1":24210, "Ka2":24002, "Kb1":27276},
    "Sn":  {"Ka1":25271, "Ka2":25044, "Kb1":28486},
    "Sb":  {"Ka1":26359, "Ka2":26111, "Kb1":29726},
    # L-lines (selected)
    "Au":  {"La1": 9713, "Lb1":11443},
    "Pb":  {"La1":10551, "Lb1":12614},
    "Pt":  {"La1": 9442, "Lb1":11071},
    "Hg":  {"La1":10010, "Lb1":11824},
    "Bi":  {"La1":10839, "Lb1":13024},
}

def emission_energy_eV(element: str, line: str) -> float | None:
    """Return the known emission energy in eV for element+line, or None if unknown."""
    elem = element.strip().capitalize()
    ln   = line.strip()
    return EMISSION_LINES.get(elem, {}).get(ln, None)

def energy_to_bin(energy_eV: float, n_bins: int = 4096,
                  emin_eV: float = 0.0, emax_eV: float = 40960.0) -> int:
    """Linear mapping from photon energy → detector bin index.

    The Ge detector at BioXAS uses a linear ADC mapping.
    Default range 0–40960 eV covers the full 4096-bin MCA at 10 eV/bin.
    Adjust emin_eV / emax_eV if your detector is calibrated differently.
    """
    eV_per_bin = (emax_eV - emin_eV) / n_bins
    return int(round((energy_eV - emin_eV) / eV_per_bin))

def auto_roi(element: str, line: str, half_width: int,
             emin_eV: float, emax_eV: float) -> tuple:
    """Compute ROI automatically.

    Strategy
    --------
    1. Look up the known emission energy for element+line.
    2. Convert to an expected bin using the detector calibration.
    3. If a spectrum is loaded, search ±3×half_width around that bin for the
       true peak (argmax of smoothed spectrum) and re-centre there.
    4. Return (roi_lo, roi_hi, status_message).
    """
    n_bins = loader._n_bins if loader._n_bins else 4096

    # --- Step 1: element lookup ---
    known_eV = emission_energy_eV(element, line)
    if known_eV is None:
        # Fallback: if spectrum is loaded just find the global max
        if loader._spectra_sum is not None:
            spec = loader._spectra_sum.mean(axis=0)
            from scipy.ndimage import uniform_filter1d
            smooth = uniform_filter1d(spec.astype(float), size=max(5, half_width//4))
            centre = int(np.argmax(smooth))
            lo = max(0, centre - half_width)
            hi = min(n_bins - 1, centre + half_width)
            return lo, hi, (f"⚠️ '{element} {line}' not in database — "
                            f"used global spectral peak at bin {centre}. "
                            f"ROI [{lo}, {hi}]")
        return 0, n_bins - 1, f"❌ '{element} {line}' not in database and no spectrum loaded."

    # --- Step 2: expected bin from calibration ---
    expected_bin = energy_to_bin(known_eV, n_bins, emin_eV, emax_eV)
    expected_bin = max(0, min(n_bins - 1, expected_bin))

    # --- Step 3: peak refine if spectrum available ---
    if loader._spectra_sum is not None:
        spec   = loader._spectra_sum.mean(axis=0)
        search = max(5, 3 * half_width)
        lo_s   = max(0, expected_bin - search)
        hi_s   = min(n_bins - 1, expected_bin + search)
        window = spec[lo_s:hi_s + 1].astype(float)
        from scipy.ndimage import uniform_filter1d
        smooth = uniform_filter1d(window, size=max(3, half_width // 4))
        centre = lo_s + int(np.argmax(smooth))
        method = "peak-refined"
    else:
        centre = expected_bin
        method = "lookup only (no spectrum loaded)"

    lo = max(0, centre - half_width)
    hi = min(n_bins - 1, centre + half_width)
    return (lo, hi,
            f"✅ Auto ROI for {element} {line}: {known_eV} eV → bin {centre} ({method}). "
            f"ROI [{lo}, {hi}]  (±{half_width} bins)")


# ==============================================================================
# PART ONE: CORE LOGIC & DYNAMIC PATHING
# ==============================================================================


def project_number(project_id: str, session: str = None, subdir: str = None, custom_dir: str = None, must_exist: bool = True) -> Path:
    project_id = str(project_id).strip()
    
    # 1. Custom Directory Priority
    if custom_dir and custom_dir.strip():
        custom_path = Path(custom_dir.strip())
        if custom_path.exists():
            return custom_path

    # 2. Logic for Linux Paths
    if session:
        # Format: /beamlinedata/BIOXAS-MAIN/projects/prj39M14246/raw/session_032187
        path = (Path("/beamlinedata/BIOXAS-MAIN/projects") / f"prj{project_id}" / 
                "raw" / f"session_{session}" / "epics")
    else:
        # Standard Spectroscopy path
        path = (Path("/beamlinedata/BIOXAS-SPECTROSCOPY/projects") / f"prj{project_id}" / 
                "main" / "raw" / "acquamanData" / "exportData" / "BioXAS")
    
    # 3. Fallback to Windows Z: drive if Linux path doesn't exist
    if not path.is_dir():
        if session:
            # Updated to include BIOXAS-MAIN in the Windows path string
            path = Path(r"Z:\BIOXAS-MAIN\projects") / f"prj{project_id}" / "raw" / f"session_{session}" / "epics"
        else:
            # Fallback for spectroscopy data on Z: drive
            path = Path(r"Z:\BIOXAS-SPECTROSCOPY\projects") / f"prj{project_id}" / "main" / "raw" / "acquamanData" / "exportData" / "BioXAS"

    # 4. Apply subdirectory if requested
    print("-" * 55)
    # .as_posix() forces forward slashes in the console output
    print(f"Using path -> {path}")
    print("-" * 55)
    
    if must_exist and not path.is_dir():
        # If still not found, we return the path anyway but the scan will fail gracefully
        pass
        
    return path

def get_processed_path(project_id: str, custom_dir: str = None, save_dir: str = None) -> Path:
    """Dynamic path logic: Sets Gradio Temp Dir to a folder inside the User's Save Directory."""
    global project_processed_path
    
    if save_dir and save_dir.strip():
        path = Path(save_dir.strip())
    elif custom_dir and custom_dir.strip():
        path = Path(custom_dir.strip()) / "processed"
    else:
        path = Path("/beamlinedata/BIOXAS-SPECTROSCOPY/projects") / f"prj{project_id.strip()}" / "main" / "processed"
    
    if not path.exists():
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            user_name = os.environ.get("USER", "user")
            path = Path("/tmp") / user_name / f"prj{project_id}" / "processed"
            os.makedirs(path, exist_ok=True)
    
    gradio_tmp = path / ".gradio_cache"
    os.makedirs(gradio_tmp, exist_ok=True)
    os.environ["GRADIO_TEMP_DIR"] = str(gradio_tmp)
    
    project_processed_path = path
    return path

class XASDeglitcher:
    def __init__(self, window=5, threshold=9):
        self.window, self.threshold = int(window), float(threshold)

    def _local_stats(self, data):
        n = len(data)
        med, sigma = np.zeros(n), np.zeros(n)
        for i in range(self.window, n - self.window):
            local = data[i - self.window:i + self.window + 1]
            median = np.median(local)
            mad = np.median(np.abs(local - median))
            med[i], sigma[i] = median, (0.165325 * mad if mad > 0 else 0.0)
        return med, sigma

    def process(self, x, y, region=None, custom_val=None, E0=None):
        if region in [None, "None"]: return y
        
        y_corr = y.copy()
        target_ranges = []

        if region == "custom" and custom_val:
            try:
                val = eval(custom_val)
                if isinstance(val, dict):
                    target_ranges = list(val.values())
                elif isinstance(val, tuple):
                    target_ranges = [val]
            except: pass
        elif E0:
            if region == "pre": target_ranges = [(x.min(), E0 - 30)]
            elif region == "xanes": target_ranges = [(E0 - 30, E0 + 40)]
            elif region == "exafs": target_ranges = [(E0 + 100, x.max())]

        if not target_ranges: return y

        for r_min, r_max in target_ranges:
            mask_idx = np.where((x >= r_min) & (x <= r_max))[0]
            if len(mask_idx) == 0: continue
            
            med, sigma = self._local_stats(y_corr)
            bad_idx = [i for i in mask_idx if i >= self.window and i < len(y_corr)-self.window 
                       and sigma[i] > 0 and abs(y_corr[i] - med[i]) / sigma[i] > self.threshold]
            
            if not bad_idx: continue
            
            clean_mask = np.ones_like(y_corr, dtype=bool)
            clean_mask[bad_idx] = False
            if np.any(clean_mask):
                f = PchipInterpolator(x[clean_mask], y_corr[clean_mask], extrapolate=False)
                y_corr[bad_idx] = f(x[bad_idx])
        
        # Ensure finite values before final return
        return np.interp(x, x[np.isfinite(y_corr)], y_corr[np.isfinite(y_corr)])


class XASLoader:
    def __init__(self):
        self.project_id, self.session = "", None
        self.custom_directory = None
        self._raw_cache = {}
        self.metadata_header = []
        self.roi_lo = 0       # low emission-channel bin for ROI sum
        self.roi_hi = 4095    # high emission-channel bin for ROI sum
        self._spectra_sum = None  # (N, 4096) summed over 32 Ge elements
        self._n_bins = 4096

    def _load_data(self, fname, elem, line, bank):
        # 1. Get the base path (Root directory)
        session_path = project_number(
            self.project_id, 
            self.session, 
            subdir=None, 
            custom_dir=self.custom_directory
        )

        self.metadata_header = [] 
        
        if fname.endswith('.dat'):
            # --- FLEXIBLE .DAT PATHING ---
            # If a session is active, look in 'pandas'. Otherwise, look in root.
            if self.session:
                path = session_path / "pandas" / fname
            else:
                path = session_path / fname
            
            # Final verification to prevent FileNotFoundError
            if not path.exists():
                # Try the opposite location as a fallback before failing
                fallback = session_path / fname if self.session else session_path / "pandas" / fname
                if fallback.exists():
                    path = fallback
                else:
                    raise FileNotFoundError(f"Could not find {fname} in {session_path} or its subfolders.")

            bank_map = {'InB': {}, 'OutB': {}}
            sections = {"top": [], "endstation": [], "roi": [], "scanned": []}
            capture_mode = None
            
            with open(path, 'r') as f:
                for line_str in f:
                    if line_str.startswith('#'):
                        clean = line_str.strip()
                        if any(x in clean for x in ["XDI/1.0", "Element.symbol", "Element.edge"]):
                            sections["top"].append(clean)
                        if "# Endstation" in clean:
                            capture_mode = "endstation"
                        elif "# Regions Of Interest" in clean:
                            capture_mode = "roi"
                        elif "# Scanned Regions" in clean:
                            capture_mode = "scanned"
                        elif "Column." in clean or "Dark Current" in clean:
                            capture_mode = None

                        if capture_mode == "endstation":
                            sections["endstation"].append(clean)
                        elif capture_mode == "roi":
                            sections["roi"].append(clean)
                        elif capture_mode == "scanned":
                            if any(x in clean for x in ["# Scanned Regions", "# Start:", "#"]):
                                sections["scanned"].append(clean)

                        if "Column." in line_str:
                            p = line_str.split(':')
                            idx, lbl = int(p[0].split('.')[-1]), p[-1].strip()
                            m = re.search(fr'{elem}{line}_spectra(\d+)_{bank}_DarkCorrect', lbl)
                            if m: bank_map[bank][int(m.group(1))] = idx
                    else: 
                        break

            self.metadata_header = (sections["top"] + sections["endstation"] + 
                                   sections["roi"] + sections["scanned"])

            df = pd.read_csv(path, sep=r'\s+', comment='#', header=None).iloc[3:].reset_index(drop=True)
            val_EC = np.zeros((len(df), 32))
            for d_idx, c_idx in bank_map.get(bank, {}).items():
                if c_idx < df.shape[1]: val_EC[:, d_idx-1] = df.iloc[:, c_idx].values
            
            self._raw_cache = {
                'energy': df.iloc[:, 0].values, 
                'i0': df.iloc[:, 3].values, 
                'i1': df.iloc[:, 4].values, 
                'i2': df.iloc[:, 5].values, 
                'val_EC': val_EC,
                'gate': np.ones(len(df))
            }

        else:
            # --- .H5 STRUCTURE ---
            ecan_file = session_path / fname
            ecan_prefix = "/entry/data/"

            try:
                with h5py.File(ecan_file, 'r') as fe:

                    def get_h5_val(f, *names, prefix=""):
                        """Try multiple key names; return first that loads successfully."""
                        for name in names:
                            path = f"{prefix}{name}" if not name.startswith('/') else name
                            if path not in f:
                                continue
                            try:
                                data = f[path][()]
                                if np.issubdtype(np.array(data).dtype, np.number):
                                    return np.array(data, dtype=float)
                            except Exception:
                                continue
                        return None

                    # --- Energy axis ---
                    energy = get_h5_val(fe, "energy_encoder", "mono-energy", prefix=ecan_prefix)
                    if energy is None:
                        raise KeyError(f"Could not find energy dataset in {fname}")

                    # --- Ion chambers (try legacy short names, then panda names) ---
                    i0 = get_h5_val(fe, "I0", "panda_ion_chambers-i0-value", prefix=ecan_prefix)
                    i1 = get_h5_val(fe, "I1", "panda_ion_chambers-i1-value", prefix=ecan_prefix)
                    i2 = get_h5_val(fe, "I2", "panda_ion_chambers-i2-value", prefix=ecan_prefix)

                    # --- Gate (dots vs underscores) ---
                    gate = get_h5_val(fe, "PCAP.GATE_DURATION.Value", "PCAP_GATE_DURATION_Value",
                                      prefix=ecan_prefix)
                    if gate is None:
                        gate = np.ones_like(energy)

                    # --- Fluorescence: 3-D spectra (N, 32, 4096) ---
                    det_key = "inb" if bank.lower() == "inb" else "outb"
                    xsp_prefix = f"xsp3_{det_key}"
                    det_path = f"{ecan_prefix}xsp3_{det_key}"

                    spectra_3d = None
                    try:
                        raw = fe[det_path][:]
                        candidate = np.array(raw, dtype=float)
                        if candidate.ndim == 3:
                            spectra_3d = candidate
                            print(f"  [H5] Loaded 3D spectra {det_path}: shape={spectra_3d.shape}")
                        else:
                            print(f"  [H5] Unexpected shape at {det_path}: {candidate.shape}")
                    except Exception as e:
                        print(f"  [H5] Could not load 3D spectra from {det_path}: {e}")

                    min_len = len(energy)

                    if spectra_3d is not None:
                        # spectra_3d: (N, 32, 4096) — axis 1 = detector elements, axis 2 = emission bins
                        n_pts, n_elem, n_bins = spectra_3d.shape
                        min_len = min(len(energy), n_pts)
                        energy     = energy[:min_len]
                        spectra_3d = spectra_3d[:min_len]

                        roi_lo = int(self.roi_lo) if self.roi_lo is not None else 0
                        roi_hi = int(self.roi_hi) if self.roi_hi is not None else n_bins - 1
                        roi_lo = max(0, min(roi_lo, n_bins - 1))
                        roi_hi = max(roi_lo, min(roi_hi, n_bins - 1))

                        # Sum over the ROI emission-bin window → (N, 32): one value per element per scan point
                        val_EC = spectra_3d[:, :, roi_lo:roi_hi + 1].sum(axis=2)
                        # Average over elements → (N, 4096) for the ROI spectrum preview
                        self._spectra_sum = spectra_3d.sum(axis=1)
                        self._n_bins = n_bins

                    else:
                        # --- Fallback: pre-computed ROI sums (no per-element channels available) ---
                        roi_keys = sorted(
                            k for k in fe[ecan_prefix].keys()
                            if k.startswith(f"{xsp_prefix}-roi_sums-roi") and k.endswith("_sum")
                        )
                        if not roi_keys:
                            raise KeyError(
                                f"No 3-D spectra or ROI sums found for '{det_key}' in {fname}. "
                                f"Available keys: {list(fe[ecan_prefix].keys())}"
                            )

                        print(f"  [H5] 3D spectra unavailable — using {len(roi_keys)} ROI sums for {det_key}")
                        # Each ROI sum is already integrated over all 32 elements for one energy window.
                        # Load them into the first N_rois columns; the channel grid will show them
                        # and auto-exclude zero-filled columns.
                        val_EC = np.zeros((min_len, 32))
                        for j, rkey in enumerate(roi_keys[:32]):
                            try:
                                col = np.array(fe[f"{ecan_prefix}{rkey}"][()], dtype=float)
                                val_EC[:min_len, j] = col[:min_len]
                            except Exception:
                                pass

                        self._spectra_sum = None
                        self._n_bins = 4096

                    # Cache (skip first 3 ramp-up points)
                    # Truncate all arrays to min_len before slicing
                    sl = slice(3, None)
                    n_sl = min_len - 3
                    i0   = i0[:min_len]   if i0   is not None else None
                    i1   = i1[:min_len]   if i1   is not None else None
                    i2   = i2[:min_len]   if i2   is not None else None
                    gate = gate[:min_len] if gate is not None else None
                    self._raw_cache = {
                        'energy': energy[sl],
                        'val_EC': val_EC[sl],
                        'i0':   i0[sl]   if i0   is not None else np.zeros(n_sl),
                        'i1':   i1[sl]   if i1   is not None else np.zeros(n_sl),
                        'i2':   i2[sl]   if i2   is not None else np.zeros(n_sl),
                        'gate': gate[sl] if gate is not None else np.ones(n_sl),
                    }

            except Exception as e:
                print(f"Error loading H5 file from {ecan_file}: {e}")
                raise e

# Initialize
loader = XASLoader()

# ==============================================================================
# PART TWO: EXAFS TOOLKIT
# ==============================================================================

class EXAFSToolkit:
    def __init__(self, data_input):
        try:
            if isinstance(data_input, pd.DataFrame):
                df = data_input.copy()
            else:
                # 1. Read file and handle potential headers
                df = pd.read_csv(data_input, sep=r'\s+', comment='#', header=None)
                # 2. Force conversion to numeric and drop text/NaN rows
                df = df.apply(pd.to_numeric, errors='coerce').dropna().reset_index(drop=True)

            if df.empty:
                raise ValueError("No numeric data found in file.")

            # 3. CRITICAL: Strictly Increasing fix for Larch splines
            df = df.sort_values(by=df.columns[0]).drop_duplicates(subset=df.columns[0])

            self.group = Group(
                energy=df.iloc[:, 0].values.astype(float), 
                mu=df.iloc[:, 1].values.astype(float), 
                groupname='EXAFS'
            )
            
        except Exception as e:
            print(f"EXAFS Plotting Error: {e}")
            raise e
        
    def process_and_plot(self, tab, mu_type, p1, p2, npre, nv, n1, n2, nnorm, e0_s, rb, clo, chi_cl, kw, kmin, kmax, dk, win, rmin, rmax):
        g = self.group
        pre_edge(g, pre1=float(p1), pre2=float(p2), npre=int(npre), nvict=int(nv), norm1=float(n1), norm2=float(n2), nnorm=int(nnorm))
        g.e0 += float(e0_s)
        autobk(g, rbkg=float(rb), kweight=float(kw), clamp_lo=float(clo), clamp_hi=float(chi_cl))
        xftf(g, kmin=float(kmin), kmax=float(kmax), kweight=float(kw), dk=float(dk), window=str(win).lower())
        
        fig = go.Figure()
        
        if tab == "Mutran":
            if mu_type == 'ALL':
                fig.add_trace(go.Scatter(x=g.energy, y=g.mu, name="mu(E) Raw", line=dict(color='black')))
                fig.add_trace(go.Scatter(x=g.energy, y=g.pre_edge, name="Pre-edge", line=dict(dash='dash', color='red')))
                fig.add_trace(go.Scatter(x=g.energy, y=g.post_edge, name="Post-edge", line=dict(dash='dash', color='blue')))
                if hasattr(g, 'bkg'): 
                    fig.add_trace(go.Scatter(x=g.energy, y=g.bkg, name="mu0(E) Bkg", line=dict(color='green', width=2)))
                fig.add_vline(x=g.e0, line_dash="dot", line_color="orange", annotation_text=f"E0: {g.e0:.2f}")
                fig.update_layout(title="XAFS Data & Background Subtraction", xaxis_title="Energy (eV)", yaxis_title="Intensity")
            elif mu_type == 'Normalized': 
                fig.add_trace(go.Scatter(x=g.energy, y=g.norm, name="Normalized", line=dict(color='blue')))
                fig.update_layout(title="Normalized Mu", xaxis_title="Energy (eV)", yaxis_title="Normalized Intensity")
            else: 
                fig.add_trace(go.Scatter(x=g.energy, y=g.flat, name="Flattened", line=dict(color='green')))
                fig.update_layout(title="Flattened Mu", xaxis_title="Energy (eV)", yaxis_title="Intensity")
                
        elif tab == "K-space":
            chi_k = g.chi * g.k**float(kw)
            fig.add_trace(go.Scatter(x=g.k, y=chi_k, name="Raw", line=dict(color='black'), opacity=0.3))
            fig.add_trace(go.Scatter(x=g.k, y=savgol_filter(chi_k, 11, 3), name="Smoothed", line=dict(color='blue')))
            if hasattr(g, 'kwin'): 
                fig.add_trace(go.Scatter(x=g.k, y=g.kwin * np.max(chi_k), name=f"{win} Window", line=dict(dash='dash', color='red')))
            fig.update_layout(xaxis_title="k (1/Å)", yaxis_title=f"k^{kw} χ(k)")
            
        elif tab == "R-space":
            fig.add_trace(go.Scatter(x=g.r, y=g.chir_mag, name="Magnitude", line=dict(color='red', width=2)))
            fig.update_xaxes(range=[rmin, rmax])
            fig.update_layout(xaxis_title="R (Å)", yaxis_title="Magnitude")
            
        elif tab == "Wavelet":
            cauchy_wavelet(g, kweight=float(kw))
            fig = go.Figure(data=go.Heatmap(
                z=g.wcauchy_mag,
                x=g.k,
                y=g.r,
                colorscale='Viridis'
            ))
            fig.update_layout(title="Wavelet Magnitude", xaxis_title="k (1/Å)", yaxis_title="R (Å)")

        fig.update_layout(template="plotly_white", hovermode="x unified")
        return fig, g

# ==============================================================================
# PART THREE: REDUCTION & INTERFACE
# ==============================================================================

def export_exafs_results(g, mode, fname, kw, prj_id, custom_dir, save_dir):
    if g is None: return "⚠️ No data processed yet."
    try:
        out_dir = get_processed_path(prj_id, custom_dir, save_dir)
        final_path = out_dir / fname
        if mode == 'Normalized Mu':
            df = pd.DataFrame({'Energy': g.energy, 'Normalized_Mu': g.norm, 'Background': g.bkg})
        elif mode == 'K-space (chi)':
            df = pd.DataFrame({'k': g.k, 'chi': g.chi, 'chi_weighted': g.chi * g.k**float(kw)})
        elif mode == 'R-space (Mag)':
            df = pd.DataFrame({'R': g.r, 'Chir_Mag': g.chir_mag})
        else:
            return "⚠️ Select a valid export mode."
        df.to_csv(final_path, sep='\t', index=False)
        return f"✅ Exported to {final_path}"
    except Exception as e:
        return f"❌ Export failed: {e}"

def build_roi_plot(roi_lo, roi_hi):
    """Build a Plotly figure showing the average emission spectrum with the ROI shaded."""
    if loader._spectra_sum is None:
        return go.Figure().update_layout(title="Load an .h5 file first to see emission spectrum")
    spec = loader._spectra_sum.mean(axis=0)   # average over scan points -> (4096,)
    bins = np.arange(len(spec))
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=bins, y=spec, mode='lines', name='Emission spectrum',
                             line=dict(color='steelblue', width=1.5)))
    lo, hi = int(roi_lo), int(roi_hi)
    fig.add_vrect(x0=lo, x1=hi, fillcolor='rgba(255,165,0,0.25)',
                  layer='below', line_width=0, annotation_text="ROI",
                  annotation_position="top left")
    fig.update_layout(
        template='plotly_white',
        xaxis_title='Emission channel (bin)',
        yaxis_title='Counts (avg over scan)',
        title=f'Emission Spectrum  —  ROI [{lo} : {hi}]',
        height=300,
        margin=dict(t=40, b=40, l=50, r=20),
    )
    return fig

def gradio_auto_roi(element, line, half_width, emin_eV, emax_eV,
                    files, bank, prj_id, custom_dir):
    """Gradio callback: compute auto ROI, reload data, return updated controls + plot."""
    lo, hi, msg = auto_roi(element, line, int(half_width), float(emin_eV), float(emax_eV))
    loader.roi_lo = lo
    loader.roi_hi = hi
    if files:
        loader.project_id = str(prj_id).strip()
        loader.custom_directory = custom_dir
        try:
            loader._load_data(files[-1], element, line, bank)
        except Exception as e:
            msg += f"  ⚠️ Reload error: {e}"
    return lo, hi, build_roi_plot(lo, hi), msg

def apply_roi(roi_lo, roi_hi, files, el, li, bank, prj_id, custom_dir):
    """Update the loader's ROI bounds, reload the last-selected file, return updated ROI plot."""
    loader.roi_lo = int(roi_lo)
    loader.roi_hi = int(roi_hi)
    # Reload to refresh val_EC with the new ROI
    if files:
        loader.project_id = str(prj_id).strip()
        loader.custom_directory = custom_dir
        try:
            loader._load_data(files[-1], el, li, bank)
        except Exception as e:
            return build_roi_plot(roi_lo, roi_hi), f"⚠️ Reload error: {e}"
    return build_roi_plot(roi_lo, roi_hi), f"✅ ROI set to bins [{int(roi_lo)}, {int(roi_hi)}]"

def run_reduction(files, mode, dg_mode, dg_custom, bank, offset, exp_style, ename, el, li, chan_map, plot_opt, leg_choice, do_norm, to_exafs_check, prj_id, custom_dir, save_dir):
    if not files: return gr.update(visible=True), gr.update(visible=False), "⚠️ Select files.", None
    
    fig = go.Figure()
    all_y, base_energy = [], None
    
    try:
        out_dir = get_processed_path(prj_id, custom_dir, save_dir)

        for i, fname in enumerate(files):
            loader.project_id = str(prj_id).strip()
            loader.custom_directory = custom_dir
            loader._load_data(fname, el, li, bank)
            
            if i == 0:
                base_energy = loader._raw_cache['energy']
                meta = loader.metadata_header
                meta.insert(0, f"# Export_Path: {out_dir}")

            file_chans = chan_map.get(fname, [str(c) for c in range(1, 33)])
            idx_list = [int(c)-1 for c in file_chans]
            
            # Physics Calculation
            if mode == 'transmission': 
                y = np.log(loader._raw_cache['i0'] / (loader._raw_cache['i1'] + 1e-12))
            elif mode == 'reference': 
                y = np.log(loader._raw_cache['i1'] / (loader._raw_cache['i2'] + 1e-12))
            else: 
                y = np.nansum(loader._raw_cache['val_EC'][:, idx_list], axis=1) / (loader._raw_cache['i0'] + 1e-12)
            
            # Deglitching
            e_raw = loader._raw_cache['energy']
            e0 = e_raw[np.argmax(np.gradient(y, e_raw))]
            if dg_mode != "None": 
                y = XASDeglitcher().process(e_raw, y, region=dg_mode.lower(), custom_val=dg_custom, E0=e0)
            
            # SANITIZATION: Force sorting for strictly increasing energy
            idx_sort = np.argsort(e_raw)
            e_clean = e_raw[idx_sort]
            y_clean = y[idx_sort]
            mask = np.concatenate(([True], np.diff(e_clean) > 0))
            
            y_interp = np.interp(base_energy, e_clean[mask], y_clean[mask])
            
            if do_norm == "ON": 
                y_interp = (y_interp - np.min(y_interp)) / (np.max(y_interp) - np.min(y_interp) + 1e-12)
            
            if plot_opt in ["Individual", "ALL"]:
                fig.add_trace(go.Scatter(x=base_energy, y=y_interp + (i * offset), name=f"{fname}"))
            all_y.append(y_interp)

        avg_y = np.mean(all_y, axis=0)
        export_msg = ""
        
        # Bridge to EXAFS
        bridge_data = pd.DataFrame({'Energy': base_energy, 'mu': avg_y}).sort_values('Energy').drop_duplicates('Energy')

        # Export Logic
        if exp_style != "None":
            final_path = out_dir / ename
            df_out = pd.DataFrame({'Energy': base_energy})
            if exp_style in ['Indiv', 'All']:
                for idx, fn in enumerate(files): df_out[fn] = all_y[idx]
            if exp_style in ['Sum', 'All']: 
                col_label = "mu_trans" if mode == 'transmission' else "mu_ref" if mode == 'reference' else "mu_fluo"
                df_out[col_label] = avg_y
            
            sep = '\t' if ename.endswith('.dat') else ','
            with open(final_path, 'w') as f:
                for l in meta: f.write(f"{l}\n")
                f.write("#\n")
                df_out.to_csv(f, index=False, sep=sep)
            export_msg = f" | ✅ Saved to {final_path}"
            
        if plot_opt in ["Sum", "ALL"]:
            y_sum = avg_y + (len(files)*offset if plot_opt=="ALL" else 0)
            fig.add_trace(go.Scatter(x=base_energy, y=y_sum, name=f"Avg {mode.capitalize()}", line=dict(color='black', width=3)))
        
        leg_layout = dict(orientation="v", yanchor="top", y=1, xanchor="left", x=1.02) if leg_choice == "Outside" else dict(x=0.02, y=0.98)
        
        fig.update_layout(
            template="plotly_white", xaxis_title="Energy (eV)", yaxis_title="Intensity",
            hovermode="x unified", legend=leg_layout
        )
        
        return gr.update(visible=False), gr.update(visible=True, value=fig), f"Processed {len(files)} files ({mode}).{export_msg}", bridge_data

    except Exception as e:
        return gr.update(visible=True), gr.update(visible=False), f"❌ Error: {str(e)}", None

# ==============================================================================
# UI BLOCK
# ==============================================================================

custom_theme = gr.themes.Soft(
    primary_hue="gray", secondary_hue="gray", neutral_hue="slate",
    font=[gr.themes.GoogleFont("Poppins"), "Arial", "sans-serif"],)

with gr.Blocks(theme=custom_theme) as BioXAS:
    channel_state = gr.State({})
    exafs_group_state = gr.State(None)
    cache_file_state = gr.State(None)

    gr.HTML("<h2 style='text-align: center; color: #27AE60;'>BioXAS Data Reduction</h2>")
    
    with gr.Tabs():
        with gr.Tab("1. Reduction"):
            with gr.Row():
                with gr.Column(scale=1):
                    with gr.Row():
                        prj = gr.Textbox(label="Project ID", placeholder= "xxxxxxxx")
                        custom_path = gr.Textbox(label="Local Folder Path", placeholder="Optional source path...")
                    save_path = gr.Textbox(label="Data Save Directory", placeholder="Empty = default processed folder")
                    sess = gr.Textbox(label="Session (Optional)")
                    btn_s = gr.Button("🔍 Scan Folder", variant="secondary")
                    with gr.Row():
                        el = gr.Textbox(label="Element", value="Cu", scale=1)
                        li = gr.Textbox(label="Line", value="Ka1", scale=1)
                        bk = gr.Dropdown(["OutB", "InB"], value="OutB", label="Board", scale=1)
                    fls = gr.Dropdown(label="Files", multiselect=True)
                    chs = gr.CheckboxGroup(choices=[str(i) for i in range(1, 33)], value=[str(i) for i in range(1, 33)], label="Channels")

                    with gr.Accordion("🔬 ROI (Emission Channel Window) — .h5 only", open=False):
                        gr.Markdown("**Auto ROI** uses the element/line to predict the emission peak bin, then refines by finding the true maximum in the spectrum.")
                        with gr.Row():
                            roi_half_in  = gr.Number(label="Half-width (bins)", value=50, precision=0, scale=1,
                                                     info="ROI = [peak − hw, peak + hw]")
                            roi_emin_in  = gr.Number(label="Det. E-min (eV)", value=0,     precision=0, scale=1,
                                                     info="Detector calibration low end")
                            roi_emax_in  = gr.Number(label="Det. E-max (eV)", value=40960, precision=0, scale=1,
                                                     info="Detector calibration high end (default 10 eV/bin)")
                        btn_auto_roi = gr.Button("🎯 Auto ROI", variant="primary")
                        gr.Markdown("---")
                        gr.Markdown("Or set the window manually:")
                        with gr.Row():
                            roi_lo_in = gr.Number(label="ROI Low bin",  value=0,    precision=0, scale=1)
                            roi_hi_in = gr.Number(label="ROI High bin", value=4095, precision=0, scale=1)
                        btn_roi  = gr.Button("Apply ROI & Preview", variant="secondary")
                        roi_plot = gr.Plot(label="Emission Spectrum Preview")
                        roi_log  = gr.Textbox(label="ROI Status", interactive=False)
                
                with gr.Column(scale=2):
                    with gr.Group():
                        grid = gr.Plot(label="Detector Preview", visible=True)
                        res_p = gr.Plot(label="Analysis Result", visible=False)
                    with gr.Row():
                        mode_in = gr.Radio(["fluorescence", "transmission", "reference"], value="fluorescence", label="Mode")
                        plot_choice = gr.Radio(["Individual", "Sum", "ALL"], value="ALL", label="Plotting Option")
                        leg_choice = gr.Radio(["Inside", "Outside"], value="Outside", label="Legend Position")
                    with gr.Row():
                        norm_btn = gr.Radio(["OFF", "ON"], value="ON", label="Normalization")
                        dg_in = gr.Dropdown(["None", "pre", "xanes", "exafs", "custom"], value="None", label="Deglitch")
                        dg_custom = gr.Textbox(label="Range", value='{"A": (9000, 9100)}', visible=False)
                        offset_in = gr.Number(label="Plotting Offset", value=0.1)
                    with gr.Row():
                        style = gr.Radio(["None", "Sum", "Indiv", "All"], value="None", label="Export")
                        to_exafs = gr.Radio(["None","Cache"], value="Cache", label="EXAFS Analysis Cache")
                        ename = gr.Textbox(label="Output Filename", value="BioXAS_Export.dat")
                    btn_r = gr.Button("🚀 Run Analysis & Plot", variant="primary")
                    log = gr.Textbox(label="Status")
        
        with gr.Tab("2. EXAFS Analysis"):
            with gr.Row():
                with gr.Column(scale=1):
                    with gr.Row():
                        ex_f = gr.File(label="Upload Data (.dat)", scale=3)
                        btn_cache = gr.Button("📂 Use Cache", variant="secondary", scale=1)
                    
                    with gr.Row():
                        ex_t = gr.Radio(["Mutran", "K-space", "R-space", "Wavelet"], value="Mutran", label="View", scale=2)
                        mu_view = gr.Dropdown(["ALL", "Normalized", "Flat"], value="ALL", label="Mu View Type", scale=1)
                    
                    with gr.Accordion("Pre-edge & Normalization", open=False):
                        with gr.Row():
                            p1_in = gr.Number(label="Pre1", value=-200); p2_in = gr.Number(label="Pre2", value=-30)
                        with gr.Row():
                            np_in = gr.Number(label="npre", value=1); nv_in = gr.Number(label="nvict", value=1)
                        with gr.Row():
                            n1_in = gr.Number(label="Norm1", value=150); n2_in = gr.Number(label="Norm2", value=-1)
                        with gr.Row():
                            nn_in = gr.Number(label="nnorm", value=1); e0_s_in = gr.Slider(-10, 10, 0, label="E0 Shift (eV)")

                    with gr.Accordion("Background (autobk)",open=False):
                        with gr.Row():
                            rb_in = gr.Slider(0.1, 2.5, 1.0, label="Rbkg"); kw_in = gr.Slider(0, 4, 2, step=1, label="k-weight")
                        with gr.Row():
                            clo_in = gr.Number(label="Clamp Lo", value=2.0); chi_cl_in = gr.Number(label="Clamp Hi", value=20.0)

                    with gr.Accordion("Fourier (xftf)", open=False):
                        with gr.Row():
                            kmin_in = gr.Number(label="k-min", value=2.0); kmax_in = gr.Number(label="k-max", value=10.0)
                        with gr.Row():
                            dk_in = gr.Number(label="dk", value=0.1); win_in = gr.Dropdown(["Kaiser", "Hanning", "Welch"], value="Kaiser", label="Window")
                        with gr.Row():
                            rmin_in = gr.Number(label="R-min", value=0.0); rmax_in = gr.Slider(2, 12, 6, label="R-max")
                    
                    btn_e = gr.Button("🔄 Process & Plot", variant="primary")

                with gr.Column(scale=2): 
                    ex_p = gr.Plot()
                    with gr.Group():
                        gr.Markdown("### 💾 Export EXAFS Results")
                        with gr.Row():
                            ex_mode = gr.Radio(["Normalized Mu", "K-space (chi)", "R-space (Mag)"], label="Export Mode")
                        with gr.Row():
                            ex_name = gr.Textbox(label="Export Filename", value="exafs_output.dat", scale=2)
                            btn_ex = gr.Button("Export Data", variant="secondary", scale=1)
                        ex_log = gr.Textbox(label="Export Status", interactive=False)

    # ==============================================================================
    # HELPER FUNCTIONS
    # ==============================================================================
    
    def scan_and_refresh(p, s, custom_dir, save_dir):
        p_clean = str(p).strip()
        loader.project_id, loader.session = p_clean, s
        loader.custom_directory = custom_dir
        search_path = project_number(p_clean, s, custom_dir=custom_dir)
        if not search_path.exists():
            return gr.update(choices=[]), f"❌ Error: Path not found: {search_path}"
        try:
            get_processed_path(p_clean, custom_dir, save_dir) 
            files = sorted([f for f in os.listdir(search_path) if f.endswith(('.dat', '.h5'))])
            return gr.update(choices=files), f"✅ Found {len(files)} files in prj{p_clean}."
        except Exception as e:
            return gr.update(choices=[]), f"❌ Scan Error: {str(e)}"

    def score_channels(energy, val_EC, i0):
        good_channels = []
        mu_stack = []
        for i in range(32):
            m = val_EC[:, i] / (i0 + 1e-12)
            mu_stack.append(m)
        mu_stack = np.array(mu_stack)
        median_mu = np.nanmedian(mu_stack, axis=0)
        mu_sum = np.nansum(val_EC, axis=1) / (i0 + 1e-12)
        e0 = energy[np.argmax(np.gradient(mu_sum, energy))]
        pre_mask = (energy > e0 - 150) & (energy < e0 - 30)
        post_mask = (energy > e0 + 40) & (energy < e0 + 150)
        for i in range(32):
            raw_counts, mu = val_EC[:, i], mu_stack[i]
            if np.nanmean(raw_counts) < 10 or np.nanmean(raw_counts) > 1e7: continue
            pre_data = mu[pre_mask]
            if len(pre_data) > 5:
                rms_pre = 1.4826 * np.nanmedian(np.abs(pre_data - np.nanmedian(pre_data)))
                if rms_pre > 0.5: continue
            else: continue
            mask = np.isfinite(mu) & np.isfinite(median_mu)
            if np.sum(mask) > 10 and np.corrcoef(mu[mask], median_mu[mask])[0, 1] < 0.95: continue
            if (np.nanmean(mu[post_mask]) - np.nanmean(mu[pre_mask])) / (rms_pre + 1e-9) > 15.0:
                good_channels.append(str(i+1))
        return good_channels

    def update_grid(files, e, l, b, c_state, prj_id, selected=None, custom_dir=None):
        if not files: return gr.update(), gr.update(), c_state, gr.update(), gr.update()
        target = files[-1]
        p_clean = str(prj_id).strip()
        loader.project_id = p_clean
        loader.custom_directory = custom_dir 
        loader._load_data(target, e, l, b)
        if target not in c_state:
            c_state[target] = score_channels(loader._raw_cache['energy'], loader._raw_cache['val_EC'], loader._raw_cache['i0'])
        elif selected is not None:
            c_state[target] = selected
        current_chans = c_state[target]
        sel_set = {int(ch) for ch in current_chans}
        plt.close('all')
        fig, axes = plt.subplots(4, 8, figsize=(12, 7), constrained_layout=True)
        for i in range(32):
            ax = axes.flatten()[i]
            is_on = (i+1) in sel_set
            y = loader._raw_cache['val_EC'][:, i] / (loader._raw_cache['i0'] + 1e-12)
            line_color, bg_color = ('#27AE60', '#EAF2E3') if is_on else ('#C0392B', '#FDEDEC')
            ax.plot(loader._raw_cache['energy'], y, color=line_color, lw=1.0)
            ax.set_facecolor(bg_color); ax.set_xticks([]); ax.set_yticks([])
            ax.set_title(f"CH{i+1}", fontsize=8, color=line_color, fontweight='bold')
        return fig, gr.update(value=current_chans), c_state, gr.update(visible=True), gr.update(visible=False)

    def trigger_exafs_analysis(manual_file, cache_data, *args):
        source = manual_file if manual_file is not None else cache_data
        if source is None: return None, None
        try:
            toolkit = EXAFSToolkit(source)
            return toolkit.process_and_plot(*args)
        except Exception as e:
            print(f"EXAFS Plotting Error: {e}")
            return None, None

    # ==============================================================================
    # BINDINGS
    # ==============================================================================
    dg_in.change(lambda x: gr.update(visible=(x == "custom")), dg_in, dg_custom)
    btn_s.click(scan_and_refresh, [prj, sess, custom_path, save_path], [fls, log])
    btn_roi.click(apply_roi, [roi_lo_in, roi_hi_in, fls, el, li, bk, prj, custom_path], [roi_plot, roi_log])
    btn_auto_roi.click(gradio_auto_roi,
        [el, li, roi_half_in, roi_emin_in, roi_emax_in, fls, bk, prj, custom_path],
        [roi_lo_in, roi_hi_in, roi_plot, roi_log])
    
    fls.change(update_grid, [fls, el, li, bk, channel_state, prj, gr.State(None), custom_path], [grid, chs, channel_state, grid, res_p])
    chs.change(update_grid, [fls, el, li, bk, channel_state, prj, chs, custom_path], [grid, chs, channel_state, grid, res_p])
    
    btn_r.click(run_reduction, 
                [fls, mode_in, dg_in, dg_custom, bk, offset_in, style, ename, el, li, channel_state, plot_choice, leg_choice, norm_btn, to_exafs, prj, custom_path, save_path], 
                [grid, res_p, log, cache_file_state])
    
    btn_cache.click(lambda cache: (cache, "✅ Cache Loaded") if cache is not None else (None, "⚠️ Empty Cache"), cache_file_state, [ex_f, ex_log])

    ex_params = [ex_t, mu_view, p1_in, p2_in, np_in, nv_in, n1_in, n2_in, nn_in, e0_s_in, rb_in, clo_in, chi_cl_in, kw_in, kmin_in, kmax_in, dk_in, win_in, rmin_in, rmax_in]
    
    for comp in [ex_t, mu_view, rb_in, kw_in, kmin_in, kmax_in, rmax_in, e0_s_in, win_in]:
        if hasattr(comp, "release"):
            comp.release(trigger_exafs_analysis, [ex_f, cache_file_state] + ex_params, [ex_p, exafs_group_state])
        else:
            comp.change(trigger_exafs_analysis, [ex_f, cache_file_state] + ex_params, [ex_p, exafs_group_state])
    
    btn_e.click(trigger_exafs_analysis, [ex_f, cache_file_state] + ex_params, [ex_p, exafs_group_state])
    btn_ex.click(export_exafs_results, inputs=[exafs_group_state, ex_mode, ex_name, kw_in, prj, custom_path, save_path], outputs=ex_log)

BioXAS.launch(
    share=False,
    inbrowser=True,
    allowed_paths=[
        str(Path.home()),
        str(Path.cwd()),
        base_project_path,
        "/beamlinedata/BIOXAS-SPECTROSCOPY",
        "/beamlinedata/BIOXAS-MAIN",
        "/tmp",
        os.environ.get("GRADIO_TEMP_DIR", "/tmp"),
    ]
)