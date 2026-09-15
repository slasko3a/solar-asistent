import streamlit as st
import requests
import pandas as pd
from datetime import datetime

# Osnovna konfiguracija strani
st.set_page_config(page_title="NGEN Nadzornik Špic v2", layout="centered", page_icon="☀️")

# 1. Parametri sistema in mesečna umeritev
PV_PEAK_KW = 13.94           # 34 modulov po 410 W
TILT_DEG = 35                # Naklon strehe 35°
AZIMUTH_DEG = 0              # Lega: Čisti Jug (0°)
INVERTER_MAX_KW = 12.00      # Nazivna meja razsmernika STAR-H3
GRID_SAFE_LIMIT_KW = 8.90    # NGEN meja profila oddaje
GRID_MAX_LIMIT_KW = 9.42     # Kritična meja izklopa soglasja
BATTERY_CAPACITY_KWH = 23.04 # Kapaciteta NGEN hranilnika
BATTERY_EFFICIENCY = 0.95    # Izkoristek polnjenja
MAX_BATTERY_CHARGE_KW = 5.5  # Realna polnilna moč baterije

LATITUDE = 46.52             # Razkrižje
LONGITUDE = 16.20

# Sezonska umeritev glede na vpadni kot sonca na 35° in temperature celic
MONTHLY_FACTORS = {
    1: 1.00,   # Jan
    2: 1.04,   # Feb
    3: 1.12,   # Mar (idealen kot, hladen zrak)
    4: 1.12,   # Apr
    5: 1.06,   # Maj
    6: 1.00,   # Jun (visoko sonce, vroče celice)
    7: 0.98,   # Jul (pregrevanje modulov)
    8: 1.03,   # Avg
    9: 1.09,   # Sep (idealen vpadni kot za 35°)
    10: 1.08,  # Okt
    11: 1.02,  # Nov
    12: 1.00   # Dec
}

current_month = datetime.now().month
CALIBRATION_BOOST = MONTHLY_FACTORS.get(current_month, 1.05)

st.title("☀️ NGEN Nadzornik Špic v2")
st.caption(f"v2.7: Mesečno umerjanje aktivno (Mesec: {current_month}, Faktor: {CALIBRATION_BOOST:.2f})")

# 2. Vnos stanja in časovna točka
st.subheader("1. Trenutno stanje")

now = datetime.now()
col_time1, col_time2 = st.columns(2)
with col_time1:
    sim_time = st.time_input("Ura vnosa podatkov:", value=now.time())
    calc_hour = sim_time.hour
    calc_minute = sim_time.minute
    start_time_float = calc_hour + (calc_minute / 60.0)

with col_time2:
    st.info(f"⏱️ Analiza velja za preostanek dneva: **od {calc_hour:02d}:{calc_minute:02d} do večera**.")

col1, col2 = st.columns(2)

with col1:
    st.markdown("**Hišna NGEN baterija:**")
    soc_home = st.number_input("Trenutna napolnjenost NGEN (%)", min_value=0, max_value=100, value=73, step=1)
    home_energy_needed = (BATTERY_CAPACITY_KWH * (100 - soc_home) / 100.0) / BATTERY_EFFICIENCY
    st.caption(f"Do 100 % potrebuje še: **{home_energy_needed:.1f} kWh**")

with col2:
    st.markdown("**Električni avtomobil (EV):**")
    ev_present = st.checkbox("Avto je doma (opcija za porezavo)", value=True)
    if ev_present:
        ev_cap = st.number_input("Kapaciteta baterije EV (kWh)", min_value=10.0, max_value=130.0, value=77.0, step=1.0)
        soc_ev = st.number_input("Trenutna napolnjenost EV (%)", min_value=0, max_value=100, value=80, step=1)
        ev_power = st.number_input("Moč polnjenja EV (kW)", min_value=2.0, max_value=11.0, value=6.0, step=0.5)
        ev_room = ev_cap * (100 - soc_ev) / 100.0
    else:
        ev_cap, soc_ev, ev_power, ev_room = 77.0, 100, 6.0, 0.0

# 3. Vremenski model (Open-Meteo GTI)
@st.cache_data(ttl=1800)
def fetch_weather(lat, lon, tilt, azimuth):
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&hourly=global_tilted_irradiance,temperature_2m"
        f"&tilt={tilt}&azimuth={azimuth}&timezone=auto"
    )
    return requests.get(url).json()

weather_data = fetch_weather(LATITUDE, LONGITUDE, TILT_DEG, AZIMUTH_DEG)
hourly = weather_data.get("hourly", {})
gti = hourly.get("global_tilted_irradiance", [0]*24)[:24]
temps = hourly.get("temperature_2m", [20]*24)[:24]

pv_curve = []
for h in range(24):
    rad = gti[h]
    temp = temps[h]
    cell_temp = temp + (rad / 800.0) * 25.0
    temp_loss = max(0.0, (cell_temp - 25.0) * 0.0038)
    eff = (0.92 - temp_loss) * CALIBRATION_BOOST
    kw = min(INVERTER_MAX_KW, (rad / 1000.0) * PV_PEAK_KW * eff)
    pv_curve.append(round(max(0.0, kw), 2))

# Osnovna hišna poraba (z vključeno sanitarno TČ)
base_load = [0.8 if 10 <= h <= 16 else 0.4 for h in range(24)]

# 4. Simulacija privzetega načina (Hiša -> Baterija -> Omrežje)
curr_kwh = BATTERY_CAPACITY_KWH * (soc_home / 100.0)
full_hour_float = None
spike_detected = False
spike_hours = []

for h in range(calc_hour, 24):
    h_frac = (60 - calc_minute) / 60.0 if h == calc_hour else 1.0
    surplus_p = max(0.0, pv_curve[h] - base_load[h])
    room = (BATTERY_CAPACITY_KWH - curr_kwh) / BATTERY_EFFICIENCY
    
    charge_p = min(surplus_p, MAX_BATTERY_CHARGE_KW, room / h_frac)
    curr_kwh += charge_p * h_frac * BATTERY_EFFICIENCY
    
    if curr_kwh >= (BATTERY_CAPACITY_KWH * 0.99) and full_hour_float is None:
        full_hour_float = h + (1.0 - h_frac)
        
    export_p = surplus_p - charge_p
    if export_p > GRID_SAFE_LIMIT_KW:
        spike_detected = True
        spike_hours.append(h)

# 5. Odločitev in priporočilo
st.markdown("---")
st.subheader("2. Stanje varnosti in navodila")

if not spike_detected:
    st.success("### ✅ VSE JE VARNO – Ohranite profil: **Hiša ➔ BATERIJA ➔ Omrežje**")
    st.markdown(f"""
    * **Brez tveganja za prenapetost:** Baterija sama zanesljivo blaži oddajo. Tudi če/ko bo dosegla 100 %, oddaja v omrežje ne bo presegla 8,9 kW.
    * **Predvideno končno stanje:** NGEN baterija bo do večera dosegla **{min(100.0, (curr_kwh / BATTERY_CAPACITY_KWH) * 100):.0f} %**.
    * **Avtomobil:** Polnjenje podnevi ni potrebno. Polnite ga ponoči po 22:00 v cenejšem bloku.
    """)
else:
    first_spike = min(spike_hours)
    last_spike = max(spike_hours) + 1
    st.error(f"### ⚠️ NEVARNOST PREKORATITVE med {first_spike}:00 in {last_spike}:00!")
    st.markdown(f"""
    * Baterija se bo napolnila okoli **{int(full_hour_float):02d}:{int((full_hour_float % 1) * 60):02d}**, medtem ko bo sonce še presegalo mejo oddaje.
    """)
    
    if ev_present and ev_room > 3.0:
        ev_dur = min(ev_room / ev_power, float(len(spike_hours)))
        ev_start = first_spike
        ev_end = ev_start + ev_dur
        st.markdown(f"""
        **Možnost A (Najboljša - Polnjenje EV):**
        * Profil pustite na privzetem: **Hiša ➔ Baterija ➔ Omrežje**.
        * Vklopite polnjenje avta med **{int(ev_start):02d}:00 in {int(ev_end):02d}:{int((ev_dur % 1) * 60):02d}** ({ev_power} kW).
        * Avto bo porezal točno sončno špico, NGEN baterija pa bo ob mraku še vedno polna.
        """)
    
    st.markdown(f"""
    **Možnost B (Brez avtomobila - Začasen preklop NGEN profila):**
    * V NGEN aplikaciji vklopite profil: **Hiša ➔ Omrežje (8,9 kW) ➔ Baterija**.
    * **OBVEZNO ob 14:00 preklopite nazaj** na: **Hiša ➔ Baterija ➔ Omrežje**, da popoldansko sonce baterijo mirno dopolni do 100 %.
    """)

# 6. Simulacija pretokov za graf
sim_export = []
sim_soc = []
curr_kwh_sim = BATTERY_CAPACITY_KWH * (soc_home / 100.0)

for h in range(24):
    if h < calc_hour:
        sim_export.append(None)
        sim_soc.append(None)
        continue
        
    h_frac = (60 - calc_minute) / 60.0 if h == calc_hour else 1.0
    surplus_p = max(0.0, pv_curve[h] - base_load[h])
    room = (BATTERY_CAPACITY_KWH - curr_kwh_sim) / BATTERY_EFFICIENCY
    
    charge_p = min(surplus_p, MAX_BATTERY_CHARGE_KW, room / h_frac)
    curr_kwh_sim += charge_p * h_frac * BATTERY_EFFICIENCY
    export_p = surplus_p - charge_p
    
    sim_export.append(round(export_p, 1))
    sim_soc.append(round(min(100.0, (curr_kwh_sim / BATTERY_CAPACITY_KWH) * 100.0), 1))

# 7. Grafični prikaz
st.markdown("---")
st.subheader("Potek moči in oddaje (Privzeti način)")

st.markdown("""
* 🟦 **Svetlo modra:** Proizvodnja sonca (kW)
* 🔷 **Temno modra:** Oddaja v omrežje (kW)
* 🔴 **Rdeča:** Varna meja oddaje (8,9 kW)
""")

chart_df = pd.DataFrame({
    "Ura": [f"{h:02d}:00" for h in range(24)],
    "Proizvodnja sonca (kW)": [round(x, 1) for x in pv_curve],
    "Oddaja v omrežje (kW)": sim_export,
    "Varna meja oddaje (8.9 kW)": [GRID_SAFE_LIMIT_KW] * 24,
    "Napolnjenost NGEN (%)": sim_soc
}).set_index("Ura")

active_chart = chart_df.iloc[calc_hour:]

st.line_chart(active_chart[["Proizvodnja sonca (kW)", "Oddaja v omrežje (kW)", "Varna meja oddaje (8.9 kW)"]])

st.markdown("**Predvideno polnjenje NGEN baterije do večera (%):**")
st.line_chart(active_chart[["Napolnjenost NGEN (%)"]])