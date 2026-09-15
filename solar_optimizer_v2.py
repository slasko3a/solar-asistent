import streamlit as st
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

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

MONTHLY_FACTORS = {
    1: 1.00, 2: 1.04, 3: 1.12, 4: 1.12, 5: 1.06,
    6: 1.00, 7: 0.98, 8: 1.03, 9: 1.09, 10: 1.08,
    11: 1.02, 12: 1.00
}

# Čas v Sloveniji (neodvisno od lokacije Streamlit strežnika)
slo_tz = ZoneInfo("Europe/Ljubljana")
now_slo = datetime.now(slo_tz)
current_month = now_slo.month
CALIBRATION_BOOST = MONTHLY_FACTORS.get(current_month, 1.05)

st.title("☀️ NGEN Nadzornik Špic v2")
st.caption(f"v2.8: Natančna minutaža polnosti baterije (Mesec: {current_month}, Umeritev: {CALIBRATION_BOOST:.2f})")

# 2. Vnos stanja
st.subheader("1. Trenutno stanje")

col_time1, col_time2 = st.columns(2)
with col_time1:
    sim_time = st.time_input("Ura vnosa podatkov (lokalni čas):", value=now_slo.time())
    calc_hour = sim_time.hour
    calc_minute = sim_time.minute
    start_time_float = calc_hour + (calc_minute / 60.0)

with col_time2:
    st.info(f"⏱️ Analiza velja od **{calc_hour:02d}:{calc_minute:02d}** do sončnega zahoda.")

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

# 3. Vremenski model
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

base_load = [0.8 if 10 <= h <= 16 else 0.4 for h in range(24)]

# 4. Natančna minutna simulacija polnjenja in prepoznave špic
curr_kwh = BATTERY_CAPACITY_KWH * (soc_home / 100.0)
battery_full_time_float = None
spike_start_float = None
spike_end_float = None

# Simulacija po 5-minutnih korakih od trenutnega časa naprej
step_hours = 5.0 / 60.0
sim_t = start_time_float

while sim_t < 21.0:
    h_idx = min(23, int(sim_t))
    solar_p = pv_curve[h_idx]
    load_p = base_load[h_idx]
    surplus_p = max(0.0, solar_p - load_p)

    room_kwh = (BATTERY_CAPACITY_KWH - curr_kwh) / BATTERY_EFFICIENCY

    if room_kwh > 0.05:
        # Baterija se polni
        charge_p = min(surplus_p, MAX_BATTERY_CHARGE_KW)
        curr_kwh += charge_p * step_hours * BATTERY_EFFICIENCY
        export_p = surplus_p - charge_p
    else:
        # Baterija je 100 % polna
        if battery_full_time_float is None:
            battery_full_time_float = sim_t
        charge_p = 0.0
        export_p = surplus_p

    # Preverjanje presežka nad varnostno mejo
    if export_p > GRID_SAFE_LIMIT_KW:
        if spike_start_float is None:
            spike_start_float = sim_t
        spike_end_float = sim_t + step_hours

    sim_t += step_hours

spike_detected = (spike_start_float is not None)

# 5. Odločitev in priporočilo
st.markdown("---")
st.subheader("2. Stanje varnosti in navodila")

if not spike_detected:
    st.success("### ✅ VSE JE VARNO – Ohranite profil: **Hiša ➔ BATERIJA ➔ Omrežje**")
    full_info = ""
    if battery_full_time_float:
        bf_h = int(battery_full_time_float)
        bf_m = int((battery_full_time_float % 1) * 60)
        full_info = f" (100 % bo dosegla okrog **{bf_h:02d}:{bf_m:02d}**)"
    
    st.markdown(f"""
    * **Brez nevarnosti za izpad:** Baterija sama posrka viške{full_info}. Tudi ko bo polna, proizvodnja sonca ne bo presegla varne meje 8,9 kW.
    * **Predvideno stanje ob sončnem zahodu:** **{min(100.0, (curr_kwh / BATTERY_CAPACITY_KWH) * 100):.0f} %**.
    * **Avtomobil:** Polnjenje podnevi ni potrebno. Polnite ga ponoči v cenejši tarifi.
    """)
else:
    sp_s_h = int(spike_start_float)
    sp_s_m = int((spike_start_float % 1) * 60)
    sp_e_h = int(spike_end_float)
    sp_e_m = int((spike_end_float % 1) * 60)
    
    st.error(f"### ⚠️ NEVARNOST PREKORAČITVE med {sp_s_h:02d}:{sp_s_m:02d} in {sp_e_h:02d}:{sp_e_m:02d}!")
    
    if battery_full_time_float and battery_full_time_float <= spike_start_float + 0.1:
        bf_h = int(battery_full_time_float)
        bf_m = int((battery_full_time_float % 1) * 60)
        st.markdown(f"* Baterija bo dosegla 100 % ob **{bf_h:02d}:{bf_m:02d}**. Ker bo sonce takrat še presegalo 8,9 kW, bo takoj po napolnitvi presežek udaril v omrežje.")
    else:
        st.markdown(f"* Sončna proizvodnja med {sp_s_h:02d}:{sp_s_m:02d} in {sp_e_h:02d}:{sp_e_m:02d} presega vsoto porabe in polnjenja baterije.")

    if ev_present and ev_room > 2.0:
        ev_dur = min(ev_room / ev_power, max(0.5, spike_end_float - spike_start_float))
        ev_end = spike_start_float + ev_dur
        ee_h = int(ev_end)
        ee_m = int((ev_end % 1) * 60)
        st.markdown(f"""
        **Možnost A (Priporočeno - Vklop EV):**
        * Profil pustite na: **Hiša ➔ Baterija ➔ Omrežje**.
        * Vklopite polnjenje avtomobila med **{sp_s_h:02d}:{sp_s_m:02d} in {ee_h:02d}:{ee_m:02d}** ({ev_power} kW).
        * Avto bo porezal špico, baterija pa bo ob mraku še vedno polna.
        """)
    
    st.markdown(f"""
    **Možnost B (Brez avtomobila - Preklop profila):**
    * V NGEN aplikaciji vklopite profil: **Hiša ➔ Omrežje (8,9 kW) ➔ Baterija**.
    * **OBVEZNO ob {sp_e_h:02d}:{sp_e_m:02d} preklopite nazaj** na: **Hiša ➔ Baterija ➔ Omrežje**, da se baterija v miru dopolni do 100 %.
    """)

# 6. Priprava urnih podatkov za graf
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

# 7. Grafi
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
