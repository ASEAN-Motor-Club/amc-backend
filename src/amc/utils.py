from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from django.utils import timezone

def lowercase_first_char_in_keys(obj):
    """
    Recursively traverses a dictionary or a list of dictionaries and
    transforms the keys to have their first character in lowercase.

    Args:
        obj: The dictionary or list to be transformed.

    Returns:
        A new dictionary or list with the transformed keys.
    """
    # If the object is a dictionary, process its keys and values
    if isinstance(obj, dict):
        # Create a new dictionary by iterating through the original's items
        return {
            # Transform the key: make the first character lowercase
            key[0].lower() + key[1:] if key else '':
            # Recursively call the function on the value
            lowercase_first_char_in_keys(value)
            for key, value in obj.items()
        }
    # If the object is a list, process each element
    elif isinstance(obj, list):
        # Create a new list by recursively calling the function on each element
        return [lowercase_first_char_in_keys(element) for element in obj]
    # If the object is not a dict or list, return it as is (base case)
    else:
        return obj

def format_in_local_tz(dt_aware: datetime, zone_info="Asia/Bangkok") -> str:
    """
    Converts a timezone-aware datetime to the Asia/Bangkok timezone
    and formats it into the string: "Weekday, D Month YYYY HH:MM GMT+offset".

    Args:
        dt_aware: A timezone-aware datetime object.

    Returns:
        A formatted string representing the date and time in Bangkok.
    """
    # Ensure the input datetime is timezone-aware
    if dt_aware.tzinfo is None:
        raise ValueError("Input datetime must be timezone-aware.")

    # 1. Define the target timezone
    local_tz = ZoneInfo(zone_info)

    # 2. Convert the input datetime to the target timezone
    local_dt = dt_aware.astimezone(local_tz)

    # 3. Format the timezone string. For Asia/Bangkok, .tzname() returns "+07".
    tz_str = f"GMT{local_dt.tzname()}"

    # 4. Format the rest of the datetime string and combine with the timezone
    # The day is formatted using an f-string to avoid a leading zero (e.g., "8" instead of "08")
    formatted_dt_str = local_dt.strftime(f"%A, {local_dt.day} %B %Y %H:%M")

    return f"{formatted_dt_str} {tz_str}"



def format_timedelta(td):
    """
    Converts a timedelta object into a formatted string like "1 Day, 2 hours and 30 minutes".
    """
    # Extract days, and the remaining seconds
    days = td.days
    total_seconds = td.seconds
    
    # Calculate hours, minutes, and seconds
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    # Build a list of parts of the string
    parts = []
    if days > 0:
        parts.append(f"{days} Day{'s' if days > 1 else ''}")
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours > 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes > 1 else ''}")

    # Join the parts into a final string
    if len(parts) == 0:
        return "0 seconds"
    elif len(parts) == 1:
        return parts[0]
    else:
        # Join all but the last part with ", " and add " and " before the last part
        return ', '.join(parts[:-1]) + ' and ' + parts[-1]

def get_timespan(days_ago: int = 0, num_days: int = 1) -> tuple[datetime, datetime]:
    """
    Calculates a timezone-aware start and end datetime tuple.

    The start_time is the beginning of a day (00:00:00) 'days_ago' from today.
    The end_time is the beginning of the day 'num_days' after the start_time.
    This creates a half-open interval [start_time, end_time).

    Args:
        days_ago (int): How many days in the past to start from. 
                        0 means today, 1 means yesterday. Defaults to 0.
        num_days (int): The duration of the timespan in days. Defaults to 1.

    Returns:
        tuple[datetime, datetime]: A tuple containing the timezone-aware 
                                   start and end datetimes.
    """
    # Get the current time in the project's timezone (e.g., using ZoneInfo)
    now = timezone.now()

    # Calculate the start time by finding midnight of the target day
    # .replace() preserves the timezone information from `now`
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_time = start_of_today - timedelta(days=days_ago)

    # Calculate the end time by adding the duration
    end_time = start_time + timedelta(days=num_days)

    return start_time, end_time

