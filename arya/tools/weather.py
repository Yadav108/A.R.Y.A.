import httpx


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


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

    return f"{city}: {temperature}°C, wind {windspeed} km/h, weather code {weather_code}"
