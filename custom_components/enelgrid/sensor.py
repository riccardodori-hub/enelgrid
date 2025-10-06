import logging
from datetime import datetime, timedelta

from homeassistant.components.persistent_notification import async_create
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.components.sensor import SensorEntity
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util.dt import as_utc
from homeassistant.components.sensor import SensorDeviceClass

from .const import (
    CONF_PASSWORD,
    CONF_POD,
    CONF_PRICE_PER_KWH,
    CONF_USER_NUMBER,
    CONF_USERNAME,
    DOMAIN,
)
from .login import EnelGridSession

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(days=1)  # Fetch once a day


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up enelgrid sensors from a config entry."""
    pod = entry.data[CONF_POD]
    entry_id = entry.entry_id

    consumption_sensor = EnelGridConsumptionSensor(hass, entry)
    monthly_sensor = EnelGridMonthlySensor(pod)

    hass.data.setdefault("enelgrid_monthly_sensor", {})[entry_id] = monthly_sensor

    async_add_entities([consumption_sensor, monthly_sensor])  # cost_sensor

    _LOGGER.warning(
        f"enelgrid sensors added: {consumption_sensor.entity_id}, {monthly_sensor.entity_id}"
    )

    # Immediately fetch data upon install
    hass.async_create_task(consumption_sensor.async_update())

    # Set up daily update
    async def daily_update_callback(_):
        await consumption_sensor.async_update()

    async_track_time_interval(hass, daily_update_callback, SCAN_INTERVAL)


class EnelGridConsumptionSensor(SensorEntity):
    """Main sensor to fetch and import data from enelgrid."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry_id = entry.entry_id
        self._username = entry.data[CONF_USERNAME]
        self._password = entry.data[CONF_PASSWORD]
        self._pod = entry.data[CONF_POD]
        self._numero_utente = entry.data[CONF_USER_NUMBER]
        self._price_per_kwh = entry.data[CONF_PRICE_PER_KWH]
        self._attr_name = "enelgrid Daily Import"
        self._state = None
        self.session = None

    @property
    def state(self):
        return self._state
    async def update_monthly_sensor(self, all_data_by_date, entry_id):
        """Aggiorna il sensore mensile con l'ultimo valore cumulativo disponibile."""
        monthly_sensor = self.hass.data.get("enelgrid_monthly_sensor", {}).get(entry_id)

        if not monthly_sensor:
            _LOGGER.error(f"Monthly sensor is not available for entry {entry_id}!")
            return

        if not all_data_by_date:
            _LOGGER.warning("No data available to update monthly sensor.")
            return

        # Usa solo l'ultimo valore cumulativo dell’ultimo giorno
        last_day = max(all_data_by_date.keys())
        last_point = all_data_by_date[last_day][-1]
        total_kwh = last_point["cumulative_kwh"]

        monthly_sensor.set_total(total_kwh)
        _LOGGER.info(f"Updated monthly sensor to {total_kwh} kWh (last day: {last_day})")
    async def async_update(self):
        try:
            if getattr(self, "_is_updating", False):
                _LOGGER.warning("Update already in progress, skipping.")
                return
            self._is_updating = True    
            self.session = EnelGridSession(
                self._username, self._password, self._pod, self._numero_utente
            )

            data = await self.session.fetch_consumption_data()
            data_points = parse_enel_hourly_data(data)

            if data_points:
                await self.save_to_home_assistant(
                    data_points, self._pod, self.entry_id, self._price_per_kwh
                )
                await self.update_monthly_sensor(data_points, self.entry_id)
                self._state = "Imported"
            else:
                _LOGGER.warning("No hourly data found.")
                self._state = "No data"
        except ConfigEntryAuthFailed as err:
            self._state = "Login error"
            async_create(
                self.hass,
                message=f"Login failed. Please check your credentials. {err}",
                title="EnelGrid Login Error",
            )

            await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "reauth", "entry_id": self.entry_id}, data={}
            )

        except Exception as err:
            _LOGGER.exception(f"Failed to update enelgrid data: {err}")
            self._state = "Error"
        finally:
            if self.session:
                await self.session.close()

    async def save_to_home_assistant(
        self, all_data_by_date, pod, entry_id, price_per_kwh
    ):

        object_id_kw = (
            f"enelgrid_{pod.lower().replace('-', '_').replace('.', '_')}_consumption"
        )
        statistic_id_kw = f"sensor:{object_id_kw}"

        object_id_cost = (
            f"enelgrid_{pod.lower().replace('-', '_').replace('.', '_')}_kw_cost"
        )
        statistic_id_cost = f"sensor:{object_id_cost}"

        metadata_kw = {
            "has_mean": False,
            "has_sum": True,
            "name": f"Enel {pod} Consumption",
            "source": "sensor",
            "statistic_id": statistic_id_kw,
            "unit_of_measurement": "kWh",
        }

        metadata_cost = {
            "has_mean": False,
            "has_sum": True,
            "name": f"Enel {pod} Cost",
            "source": "sensor",
            "statistic_id": statistic_id_cost,
            "unit_of_measurement": "EUR",
        }
        saved_dates = set()
        for day_date, data_points in all_data_by_date.items():
            stats_kw = []
            stats_cost = []
            if day_date in saved_dates:
                _LOGGER.warning(f"Skipping duplicate save for {day_date}")
                continue
            saved_dates.add(day_date)
            
            _LOGGER.info(f"Saving data for {day_date}: {len(data_points)} hourly points")
            cumulative_offset = await self.get_last_cumulative_kwh(statistic_id_kw)

            for point in data_points:
                stats_kw.append(
                    {
                        "start": as_utc(point["timestamp"]),
                        "sum": point["cumulative_kwh"] + cumulative_offset,
                    }
                )
                stats_cost.append(
                    {
                        "start": as_utc(point["timestamp"]),
                        "sum": point["cumulative_kwh"] * price_per_kwh,
                    }
                )

            try:
                async_add_external_statistics(self.hass, metadata_kw, stats_kw)
                async_add_external_statistics(
                    self.hass, metadata_cost, stats_cost
                )  # ✅ Push cost statistics
                _LOGGER.info(
                    f"Saved {len(stats_kw)} points for {statistic_id_kw} and {len(stats_cost)} for {statistic_id_cost}"
                )
            except HomeAssistantError as e:
                _LOGGER.exception(
                    f"Failed to save statistics for {statistic_id_kw}: {e}"
                )
                raise

    async def get_last_cumulative_kwh(self, statistic_id: str):
        """Get the last recorded cumulative kWh for a given statistic_id."""
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )

        if last_stats and statistic_id in last_stats:
            _LOGGER.info(
                f"Last recorded cumulative sum for {statistic_id}: {last_stats[statistic_id][0]['sum']}"
            )
            return last_stats[statistic_id][0]["sum"]  # Last recorded cumulative sum
        return 0.0



class EnelGridMonthlySensor(SensorEntity):
    """Monthly cumulative total sensor."""

    def __init__(self, pod):
        object_id = f"enelgrid_{pod.lower().replace('-', '_').replace('.', '_')}_monthly_consumption"
        self.entity_id = f"sensor.{object_id}"
        self._attr_name = f"Enel {pod} Monthly Consumption"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = "total_increasing"
        self._attr_native_unit_of_measurement = "kWh"
        self._state = 0
        self._attr_extra_state_attributes = {"source": "enelgrid"}

    @property
    def state(self):
        return self._state

    def set_total(self, new_total):
        self._state = new_total
        self.async_write_ha_state()


def parse_enel_hourly_data(data):
    """Estrai dati orari per ogni giorno, normalizzati in kWh e completi per 24 ore."""
    aggregations = (
        data.get("data", {}).get("aggregationResult", {}).get("aggregations", [])
    )

    hourly_aggregation = next(
        (agg for agg in aggregations if agg.get("referenceID") == "hourlyConsumption"),
        None,
    )

    if not hourly_aggregation:
        raise ValueError("No hourly consumption data found in JSON")

    validity_from = data.get("data", {}).get("aggregationResult", {}).get("validityFrom")
    validity_to = data.get("data", {}).get("aggregationResult", {}).get("validityTo")
    pod = data.get("data", {}).get("aggregationResult", {}).get("pod")

    _LOGGER.info(f"EnelGrid fetch for POD {pod}: period {validity_from} → {validity_to}")

    all_data_by_date = {}
    cumulative_offset = 0

    sorted_results = sorted(
        hourly_aggregation.get("results", []),
        key=lambda r: datetime.strptime(r["date"], "%d%m%Y"),
    )

    for day_result in sorted_results:
        date_str = day_result.get("date")
        day_date = datetime.strptime(date_str, "%d%m%Y").date()

        _LOGGER.debug(f"Parsing data for {date_str} ({day_date})")

        bin_map = {int(b["name"][1:]): b["value"] for b in day_result.get("binValues", [])}
        missing_hours = [h for h in range(1, 25) if h not in bin_map]

        if missing_hours:
            _LOGGER.warning(f"Missing hours for {day_date}: {missing_hours}")

        hourly_points = []
        running_total = cumulative_offset

        for hour in range(1, 25):  # H1 to H24
            raw_value = bin_map.get(hour, 0.0)

            # Conversione difensiva da Wh a kWh
            normalized_kwh = raw_value / 1000 if raw_value > 10 else raw_value

            hour_time = datetime.combine(day_date, datetime.min.time()) + timedelta(hours=hour - 1)
            running_total += normalized_kwh

            _LOGGER.debug(
                f"{day_date} H{hour:02d}: raw={raw_value} → kWh={normalized_kwh:.3f}"
            )

            hourly_points.append({
                "timestamp": hour_time,
                "kwh": round(normalized_kwh, 3),
                "cumulative_kwh": round(running_total, 3),
            })

        all_data_by_date[day_date] = hourly_points
        cumulative_offset = hourly_points[-1]["cumulative_kwh"]

    return all_data_by_date
