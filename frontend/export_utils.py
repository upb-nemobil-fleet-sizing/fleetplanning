def write_api_base_file_from_loaded_data(data: dict) -> dict:
    """Merge the edited in-memory base data back into the original API-shaped BaseData payload."""
    import copy

    raw_base = data.get("_rawBaseData", {})
    final_structure = copy.deepcopy(raw_base) if raw_base else {}

    def merge_list(original_list, updated_list, key="id"):
        """Merge edited records into the original API records while preserving untouched fields."""
        merged = []
        original_map = {}

        for o in original_list:
            oid = o.get("id") or o.get("Guid")
            if oid:
                original_map[str(oid)] = o

        for u in updated_list:
            uid = u.get("id") or u.get("Guid")
            uid = str(uid)

            if uid in original_map:
                merged_item = copy.deepcopy(original_map[uid])
                merged_item.update(u)
                merged.append(merged_item)
            else:
                merged.append(u)

        return merged

    final_structure["cabSchedules"] = merge_list(
        raw_base.get("cabSchedules", []),
        data.get("cabs", [])
    )

    final_structure["proSchedules"] = merge_list(
        raw_base.get("proSchedules", []),
        data.get("proSchedules", [])
    )

    final_structure["chargingPoints"] = merge_list(
        raw_base.get("chargingPoints", []),
        data.get("chargingPoints", [])
    )

    final_structure["chainingLocations"] = merge_list(
        raw_base.get("chainingLocations", []),
        data.get("chainingLocations", [])
    )

    final_structure["chainRoutes"] = merge_list(
        raw_base.get("chainRoutes", []),
        data.get("chainRoutes", [])
    )

    final_structure["chainRouteSchedules"] = merge_list(
        raw_base.get("chainRouteSchedules", []),
        data.get("chainRouteSchedules", [])
    )

    if data.get("operationArea"):
        oa_original = raw_base.get("operationAreas", [])
        oa_original = oa_original[0] if oa_original else {}

        oa_merged = copy.deepcopy(oa_original)
        oa_merged["LocationBorder"] = data["operationArea"].get("points", [])
        oa_merged["GuiltyRange"] = {
            "StartTime": data.get("startTime"),
            "EndTime": data.get("endTime")
        }

        final_structure["operationAreas"] = [oa_merged]

    return final_structure


def write_api_ride_file_from_loaded_data(data: dict) -> dict:
    """Merge edited ride requests back into the original RideData simulationSteps structure."""
    import copy

    raw_ride = data.get("_rawRideData", {})
    final_structure = copy.deepcopy(raw_ride) if raw_ride else {}

    original_steps = raw_ride.get("simulationSteps", [])
    original_map = {}

    for step in original_steps:
        rp = step.get("requestParameter", {})
        uid = rp.get("UserGuid")
        if uid:
            original_map[str(uid)] = step

    merged_steps = []

    for rr in data.get("rideRequests", []):
        uid = str(rr.get("id"))

        if uid in original_map:
            step_copy = copy.deepcopy(original_map[uid])
        else:
            step_copy = {"requestParameter": {}}

        step_copy["requestParameter"].update({
            "UserGuid": rr.get("id"),
            "CurrentLocation": rr.get("CurrentLocation"),
            "TargetLocation": rr.get("TargetLocation"),
            "targetTime": rr.get("targetTime"),
            "pickupTime": rr.get("pickupTime"),
            "needRamp": rr.get("needRamp"),
            "requestedAdults": rr.get("requestedAdults"),
            "requestedChilds": rr.get("requestedChilds"),
            "luggage": rr.get("luggage"),
            "personalPreferences": rr.get("personalPreferences"),
        })

        step_copy["SimulatedTime"] = rr.get("SimulatedTime")
        step_copy["bookProposal"] = rr.get("bookProposal")
        step_copy["createReport"] = rr.get("createReport")

        merged_steps.append(step_copy)

    final_structure["simulationSteps"] = merged_steps

    return final_structure
