import streamlit as st
import requests
import pandas as pd
from datetime import datetime, date
from zoneinfo import ZoneInfo

# Osnovna konfiguracija strani
st.set_page_config(page_title="NGEN Nadzornik Špic & EV", layout="centered", page_icon="☀️")

# 1. Parametri sistema
PV_PEAK_KW = 13.94           # 34 modulov po 410 W
TILT_DEG = 35                # Naklon strehe 35°
AZIMUTH_DEG = 0              # Lega: Čisti Jug (0°)
INVERTER_MAX_KW = 12.00      # Meja razsmernika STAR-H3
GRID_LEGAL_LIMIT_KW = 9.40   # Meja soglasja po odločbi (izklop)
BATTERY_CAPACITY_KWH = 23.04 # NGEN hranilnik
BATTERY_EFFICIENCY = 0.95    # Izkoristek baterije
MAX_BATTERY_CHARGE_KW = 10.5 # STAR-H3 polni baterijo s polno močjo (10+ kW)

LATITUDE = 46.52             # Razkrižje
LONGITUDE = 16.20

# Umeritev glede na kot sonca po mesecih
MONTHLY_FACTORS = {
    1: 0.95, 2: 1.00, 3: 1.08, 4: 1.08, 5: 1.04,
    6: 0.98, 7: 0.96, 8: 1.00, 9: 1.02, 10: 1.02,
    11: 0.98, 12: 0.95
}

# Slovenski časovni pas
slo_tz = ZoneInfo("Europe/Ljubljana")
now_slo = datetime.now(slo_tz)
current_month = now_slo.month
CALIBRATION_BOOST = MONTHLY_FACTORS.get(current_month, 1.02)

# -------------------------------------------------------------
# 2. LOGIKA NOVE OMREŽNINE IN ČASOVNIH BLOKOV V SLOVENIJI
# -------------------------------------------------------------
SLO_HOLIDAYS_FIXED = {
    (1, 1), (1, 2), (2, 8), (4, 27), (5, 1), (5, 2),
    (6, 25), (8, 15), (10, 31), (11, 1), (12, 25), (12, 26)
}

def is_workday_slo(d: date) -> bool:
    if d.weekday() >= 5:  # Sobota (5) ali Nedelja (6)
        return False
    if (d.month, d.day) in SLO_HOLIDAYS_FIXED:
        return False
    return True

def get_night_block_info(charge_date: date):
    """Vrne časovni blok med 22:00 in 06:00 za določen datum"""
    month = charge_date.month
    is_high_season = month in [11, 12, 1, 2]
    workday = is_workday_slo(charge_date)
    
    if is_high_season:
        season_name = "Višja sezona"
        block = 3 if workday else 4
    else:
        season_name = "Nižja sezona"
        block = 4 if workday else 5

    day_type = "Delovni dan" if workday else "Dela prost dan / vikend"
    return season_name, day_type, block

st.title("☀️ NGEN Nadzornik Špic & EV")
st.caption(f"v3.3: Fiksni hitrosti EV (6 kW / 11 kW) | Omrežnina | Meja soglasja: {GRID_LEGAL_LIMIT_KW} kW")

# Nastavitve dogovorjenih moči (privzeto za 3x25A priključek)
with st.expander("⚙️ Dogovorjene obračunske moči (Moj Elektro)", expanded=False):
    st.write("Vrednosti za vaše merilno mesto (3x25A). Algoritem jih samodejno uporabi:")
    col_p1, col_p2, col_p3 = st.columns(3)
    p_b1 = col_p1.number_input("Blok 1 (kW)", value=5.5, step=0.1)
    p_b2 = col_p2.number_input("Blok 2 (kW)", value=6.2, step=0.1)
    p_b3 = col_p3.number_input("Blok 3 (kW)", value=7.0, step=0.1)
    col_p4, col_p5, _ = st.columns(3)
    p_b4 = col_p4.number_input("Blok 4 (kW)", value=7.5, step=0.1)
    p_b5 = col_p5.number_input("Blok 5 (kW)", value=8.5, step=0.1)

AGREED_POWERS = {1: p_b1, 2: p_b2, 3: p_b3, 4: p_b4, 5: p_b5}

# -------------------------------------------------------------
# 3. VNOS TRENUTNEGA STANJA
# -------------------------------------------------------------
st.subheader("1. Trenutno stanje")

col_t1, col_t2 = st.columns(2)
with col_t1:
    sim_time = st.time_input("Ura vnosa podatkov:", value=now_slo.time())
    calc_hour = sim_time.hour
    calc_minute = sim_time.minute
    start_time_float = calc_hour + (calc_minute / 60.0)

with col_t2:
    st.info(f"⏱️ Analiza od **{calc_hour:02d}:{calc_minute:02d}** do sončnega zahoda.")

col_b1, col_b2 = st.columns(2)
with col_b1:
    soc_home = st.number_input("Napolnjenost NGEN (%)", min_value=0, max_value=100, value=52, step=1)
    home_kwh_needed = (BATTERY_CAPACITY_KWH * (100 - soc_home) / 100.0) / BATTERY_EFFICIENCY
    st.caption(f"Do 100 % potrebuje še: **{home_kwh_needed:.2f} kWh**")

with col_b2:
    base_home_load = st.number_input("Trenutna poraba hiše (kW)", min_value=0.2, max_value=5.0, value=0.7, step=0.1)

# EV podatki
st.markdown("**Električni avtomobil (EV):**")
col_ev1, col_ev2, col_ev3 = st.columns(3)
with col_ev1:
    ev_cap = st.number_input("Kapaciteta EV (kWh)", min_value=10.0, max_value=130.0, value=77.0, step=1.0)
with col_ev2:
    soc_ev = st.number_input("Trenutna baterija EV (%)", min_value=0, max_value=100, value=50, step=5)
with col_ev3:
    target_soc_ev = st.number_input("Želena napolnjenost EV (%)", min_value=soc_ev, max_value=100, value=80, step=5)

ev_kwh_needed = ev_cap * (target_soc_ev - soc_ev) / 100.0
st.caption(f"EV do cilja ({target_soc_ev} %) potrebuje: **{ev_kwh_needed:.1f} kWh**.")

# -------------------------------------------------------------
# 4. VREMENSKI MODEL
# -------------------------------------------------------------
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

# -------------------------------------------------------------
# 5. DNEVNA REGULACIJA ODDAJE
# -------------------------------------------------------------
rem_solar_kwh = 0.0
for h in range(calc_hour, 20):
    frac = (60 - calc_minute)/60.0 if h == calc_hour else 1.0
    rem_solar_kwh += max(0.0, pv_curve[h] - base_home_load) * frac

rem_hours = max(0.5, 18.5 - start_time_float)
ideal_export_target = max(0.0, min(8.9, (rem_solar_kwh - home_kwh_needed) / rem_hours))

st.markdown("---")
st.subheader("2. Dnevna regulacija oddaje v omrežje")

custom_grid_kw = st.slider(
    "Nastavljena prednostna oddaja v NGEN (kW):",
    min_value=0.0,
    max_value=8.9,
    value=7.0,
    step=0.5,
    help="Moč, ki jo NGEN pošilja v omrežje pred polnjenjem baterije."
)

sim_t = start_time_float
step_h = 5.0 / 60.0
curr_kwh = BATTERY_CAPACITY_KWH * (soc_home / 100.0)
full_time = None
max_grid_seen = 0.0

while sim_t < 20.5:
    h_idx = min(23, int(sim_t))
    solar_now = pv_curve[h_idx]
    surplus = max(0.0, solar_now - base_home_load)
    room = (BATTERY_CAPACITY_KWH - curr_kwh) / BATTERY_EFFICIENCY

    grid_p = min(surplus, custom_grid_kw)
    left_for_batt = surplus - grid_p

    if room > 0.05:
        batt_charge = min(left_for_batt, MAX_BATTERY_CHARGE_KW)
        curr_kwh += batt_charge * step_h * BATTERY_EFFICIENCY
        unabsorbed = left_for_batt - batt_charge
    else:
        if full_time is None:
            full_time = sim_t
        batt_charge = 0.0
        unabsorbed = left_for_batt

    total_grid_export = grid_p + unabsorbed
    max_grid_seen = max(max_grid_seen, total_grid_export)
    sim_t += step_h

final_soc = min(100.0, (curr_kwh / BATTERY_CAPACITY_KWH) * 100.0)

col_r1, col_r2, col_r3 = st.columns(3)
col_r1.metric("Napolnjenost ob mraku", f"{final_soc:.0f} %")
col_r2.metric("Najvišja oddaja", f"{max_grid_seen:.1f} kW", delta=f"{max_grid_seen - GRID_LEGAL_LIMIT_KW:.1f} kW", delta_color="inverse")
if full_time:
    col_r3.metric("Baterija 100 % ob", f"{int(full_time):02d}:{int((full_time%1)*60):02d}")
else:
    col_r3.metric("Baterija 100 % ob", "Ne bo dosegla 100 %")

if final_soc < 99.0:
    st.warning(f"""
    ⚠️ **Nastavitev {custom_grid_kw:.1f} kW je previsoka za 100 % napolnjenost!**
    * Baterija se bo ustavila pri **{final_soc:.0f} %**.
    * **Priporočilo:** Znižajte prednostno oddajo na **{ideal_export_target:.1f} kW** ali po 15:30 preklopite nazaj na samooskrbo.
    """)
elif max_grid_seen > GRID_LEGAL_LIMIT_KW:
    st.error(f"🚨 **Nevarnost izpada!** Oddaja bi presegla 9,4 kW. Zvišajte prednostno oddajo v omrežje.")
else:
    st.success(f"✅ **ODLIČNA NASTAVITEV:** Oddaja varna ({max_grid_seen:.1f} kW), baterija bo dosegla 100 %.")

# -------------------------------------------------------------
# 6. NOČNI ASISTENT ZA POLNJENJE EV (FIKSNO 6 kW / 11 kW)
# -------------------------------------------------------------
st.markdown("---")
st.subheader("3. 🌙 Nočni planer EV (Fiksni hitrosti)")

tonight_date = now_slo.date()
season_name, day_type, night_block = get_night_block_info(tonight_date)
auto_agreed_power = AGREED_POWERS[night_block]
HOUSE_NIGHT_RESERVE_KW = 0.6

st.markdown(f"""
* 📅 **Obdobje:** {season_name} ({tonight_date.strftime('%d. %m. %Y')})
* 🏷️ **Dan:** {day_type} $\\rightarrow$ Nočni blok (22:00–06:00): **ČASOVNI BLOK {night_block}**
* ⚡ **Vaša dogovorjena moč za Blok {night_block}:** **{auto_agreed_power:.1f} kW**
""")

# Izbira med dvema razpoložljivima hitrostma v vozilu
selected_speed_kw = st.radio(
    "Nastavitev moči polnjenja v avtomobilu / polnilnici:",
    options=[6.0, 11.0],
    format_func=lambda x: f"🔋 {int(x)} kW — {'Zmanjšana moč (Priporočeno za omrežnino)' if x == 6.0 else 'Polna moč (Hitro polnjenje)'}",
    horizontal=True
)

if ev_kwh_needed <= 0.5:
    st.info("🚗 Baterija avtomobila je že na želeni ravni. Nočno polnjenje ni potrebno.")
else:
    charge_duration_h = ev_kwh_needed / selected_speed_kw
    dur_h = int(charge_duration_h)
    dur_m = int((charge_duration_h % 1) * 60)
    
    end_time_raw = 22.0 + charge_duration_h
    end_h = int(end_time_raw % 24)
    end_m = int((end_time_raw % 1) * 60)
    total_night_load = selected_speed_kw + HOUSE_NIGHT_RESERVE_KW

    st.markdown(f"""
    #### 📋 Načrt za izbrano nastavitev ({int(selected_speed_kw)} kW):
    * **Čas polnjenja:** **22:00 – {end_h:02d}:{end_m:02d}** (trajanje: **{dur_h}h {dur_m}min**).
    * **Skupna obremenitev (avto + hiša):** **{total_night_load:.1f} kW** (pri dogovorjeni moči **{auto_agreed_power:.1f} kW**).
    """)

    if total_night_load > auto_agreed_power:
        st.error(f"""
        ⚠️ **PREKORAČITEV DOGOVORJENE MOČI za {total_night_load - auto_agreed_power:.1f} kW!**
        * Polnih 11 kW skupaj s hišno porabo preseže dogovorjeno moč bloka {night_block} ({auto_agreed_power:.1f} kW).
        * **Priporočilo:** Preklopite stikalo zgoraj na **6 kW**, da se izognete penalom na položnici!
        """)
    else:
        st.success(f"✅ **POPOLNOMA VARNO:** Skupna moč {total_night_load:.1f} kW je znotraj dogovorjene meje {auto_agreed_power:.1f} kW. Penali so 0 €.")

    if charge_duration_h > 8.0:
        st.warning(f"""
        ⏰ **Polnjenje se podaljša čez 06:00 zjutraj!**
        * Do 06:00 (v 8 urah) se bo nateklo **{selected_speed_kw * 8:.1f} kWh**.
        * Ker se ob 06:00 začne dražji dnevni blok z nižjo močjo, je priporočljivo v avtu nastaviti zaključek ob 06:00.
        """)

    st.warning(f"""
    🛑 **OPOZORILO ZA NGEN HRANILNIK:**
    Pred spanjem v NGEN aplikaciji pod *Nastavitve hranilnika* nastavite minimalno raven (**Min SoC**) na npr. **80 % ali 90 %**, da avto ne bo praznil hišne baterije!
    """)

# -------------------------------------------------------------
# 7. GRAFI
# -------------------------------------------------------------
st.markdown("---")
st.subheader("4. Potek moči in oddaje (Dnevni pregled)")

sim_export_hourly = []
sim_soc_hourly = []
sim_kwh_loop = BATTERY_CAPACITY_KWH * (soc_home / 100.0)

for h in range(24):
    if h < calc_hour:
        sim_export_hourly.append(None)
        sim_soc_hourly.append(None)
        continue
    h_frac = (60 - calc_minute) / 60.0 if h == calc_hour else 1.0
    surplus = max(0.0, pv_curve[h] - base_home_load)
    room = (BATTERY_CAPACITY_KWH - sim_kwh_loop) / BATTERY_EFFICIENCY
    
    grid_p = min(surplus, custom_grid_kw)
    left_b = surplus - grid_p
    
    if room > 0.05:
        ch = min(left_b, MAX_BATTERY_CHARGE_KW, room / h_frac)
        sim_kwh_loop += ch * h_frac * BATTERY_EFFICIENCY
        unabs = left_b - ch
    else:
        unabs = left_b
    
    sim_export_hourly.append(round(grid_p + unabs, 1))
    sim_soc_hourly.append(round(min(100.0, (sim_kwh_loop / BATTERY_CAPACITY_KWH) * 100.0), 1))

chart_df = pd.DataFrame({
    "Ura": [f"{h:02d}:00" for h in range(24)],
    "Proizvodnja sonca (kW)": [round(x, 1) for x in pv_curve],
    "Oddaja v omrežje (kW)": sim_export_hourly,
    "Meja odločbe (9.4 kW)": [GRID_LEGAL_LIMIT_KW] * 24,
    "Napolnjenost NGEN (%)": sim_soc_hourly
}).set_index("Ura")

active_chart = chart_df.iloc[calc_hour:]

st.line_chart(
    active_chart[["Proizvodnja sonca (kW)", "Oddaja v omrežje (kW)", "Meja odločbe (9.4 kW)"]],
    color=["#F59E0B", "#2563EB", "#DC2626"]
)

st.line_chart(active_chart[["Napolnjenost NGEN (%)"]], color=["#10B981"])
