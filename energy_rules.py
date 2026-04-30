DEFAULT_COMFORT_PROFILE = {
    "comfort_min_c": 20.0,
    "comfort_max_c": 26.0,
    "critical_temp_c": 30.0,
}

DEFAULT_OVERPOWER_LIMIT_W = 2500.0


def _to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _append_unique(items, value):
    if value and value not in items:
        items.append(value)


def _clamp_score(value):
    return max(0, min(100, int(round(value))))


def evaluate_energy_state(data, comfort_profile):
    profile = dict(DEFAULT_COMFORT_PROFILE)
    if isinstance(comfort_profile, dict):
        profile.update(comfort_profile)

    comfort_min_c = _to_float(profile.get("comfort_min_c"), DEFAULT_COMFORT_PROFILE["comfort_min_c"])
    comfort_max_c = _to_float(profile.get("comfort_max_c"), DEFAULT_COMFORT_PROFILE["comfort_max_c"])
    critical_temp_c = _to_float(profile.get("critical_temp_c"), DEFAULT_COMFORT_PROFILE["critical_temp_c"])
    overpower_limit_w = _to_float(profile.get("overpower_limit_w"), DEFAULT_OVERPOWER_LIMIT_W)

    temp_c = _to_float(data.get("temp_c"))
    presence = _to_bool(data.get("presence"))
    fan_on = _to_bool(data.get("fan_on"))
    lamp_on = _to_bool(data.get("lamp_on"))
    window_open = _to_bool(data.get("window_open"))
    temp_ok = _to_bool(data.get("temp_ok"), default=True)
    anomaly_dht_fail = _to_bool(data.get("anomaly_dht_fail"))
    power_w = _to_float(data.get("power_w"))

    reasons = []
    energy_anomaly = []

    if temp_c > comfort_max_c:
        thermal_state = "trop_chaud"
        _append_unique(reasons, "Temperature au-dessus de la zone de confort.")
    elif temp_c < comfort_min_c:
        thermal_state = "trop_froid"
        _append_unique(reasons, "Temperature en dessous de la zone de confort.")
    else:
        thermal_state = "confort"
        _append_unique(reasons, "Temperature dans la zone de confort.")

    critical_temp_alert = temp_c > critical_temp_c
    if critical_temp_alert:
        _append_unique(reasons, "Temperature critique depassee.")

    recommended_action = "none"
    auto_command = None

    if thermal_state == "trop_chaud":
        recommended_action = "fan_on"
        if presence and not fan_on:
            auto_command = "fan_on"
            _append_unique(reasons, "Presence detectee avec ventilateur inactif: ventilation automatique autorisee.")
        elif presence and fan_on:
            _append_unique(reasons, "Ventilateur deja actif, aucune commande supplementaire necessaire.")
        else:
            _append_unique(reasons, "Absence detectee: recommandation de ventilation sans auto-commande.")
    elif thermal_state == "trop_froid":
        recommended_action = "heating_alert"
        _append_unique(reasons, "Alerte chauffage recommandee.")
    else:
        _append_unique(reasons, "Aucune action de confort necessaire.")

    if not presence and lamp_on:
        _append_unique(energy_anomaly, "waste_lighting_absence")
        _append_unique(reasons, "Eclairage actif alors que la zone est inoccupee.")

    if not presence and fan_on:
        _append_unique(energy_anomaly, "waste_ventilation_absence")
        _append_unique(reasons, "Ventilation active alors que la zone est inoccupee.")

    if window_open and fan_on:
        _append_unique(energy_anomaly, "open_window_ventilation_loss")
        _append_unique(reasons, "Fenetre ouverte avec ventilation active: perte energetique detectee.")

    if power_w > overpower_limit_w:
        _append_unique(energy_anomaly, "overpower")
        _append_unique(reasons, f"Puissance au-dessus du seuil raisonnable ({int(overpower_limit_w)} W).")

    sensor_fault = anomaly_dht_fail or not temp_ok
    if sensor_fault:
        _append_unique(reasons, "Capteur thermique en defaut ou mesure non fiable.")

    comfort_score = 100
    if thermal_state != "confort":
        comfort_score -= 10
    if critical_temp_alert:
        comfort_score -= 25
    if "waste_lighting_absence" in energy_anomaly:
        comfort_score -= 15
    if "waste_ventilation_absence" in energy_anomaly:
        comfort_score -= 15
    if "open_window_ventilation_loss" in energy_anomaly:
        comfort_score -= 10
    if "overpower" in energy_anomaly:
        comfort_score -= 20
    if sensor_fault:
        comfort_score -= 20

    return {
        "thermal_state": thermal_state,
        "recommended_action": recommended_action,
        "critical_temp_alert": critical_temp_alert,
        "energy_anomaly": energy_anomaly,
        "comfort_score": _clamp_score(comfort_score),
        "auto_command": auto_command,
        "reasons": reasons,
    }
