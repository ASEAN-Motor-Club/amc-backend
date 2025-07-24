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
