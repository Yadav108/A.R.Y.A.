import httpx


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

# WMO weather codes (Open-Meteo) → words, so ARYA never says "weather code 3".
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy with rime", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "light rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "heavy snow showers", 95: "a thunderstorm",
    96: "a thunderstorm with hail", 99: "a thunderstorm with heavy hail",
}


async def geocode(city: str) -> tuple[float, float, str]:
    """City name → (latitude, longitude, display name). Only the city name is sent."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(GEOCODING_URL, params={"name": city, "count": 1, "language": "en"})
            response.raise_for_status()
            results = response.json().get("results") or []
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Couldn't look up {city}: {exc}") from exc
    if not results:
        raise RuntimeError(f"I couldn't find a place called {city}.")
    top = results[0]
    return float(top["latitude"]), float(top["longitude"]), top.get("name") or city


async def get_weather(city: str, latitude: float, longitude: float) -> str:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,weathercode,windspeed_10m",
        "timezone": "auto",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(OPEN_METEO_URL, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Failed to fetch weather for {city}: {exc}") from exc

    current = data.get("current")
    if not isinstance(current, dict):
        raise RuntimeError(f"Failed to fetch weather for {city}: invalid API response.")

    temperature = current.get("temperature_2m")
    windspeed = current.get("windspeed_10m")
    weather_code = current.get("weathercode")

    if temperature is None or windspeed is None or weather_code is None:
        raise RuntimeError(
            f"Failed to fetch weather for {city}: missing weather data in response."
        )

    conditions = _WMO.get(int(weather_code), "mixed conditions")
    return f"{city}: {conditions}, {round(float(temperature))} degrees Celsius, wind {round(float(windspeed))} kilometres per hour."
