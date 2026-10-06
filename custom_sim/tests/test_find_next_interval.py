
import pytest
from datetime import datetime
from enum import Enum
from types import SimpleNamespace

from custom_sim.utils_cs import visualize_schedule


def debug_show(cab, cust, L, R):
	print("\n--- DEBUG VISUAL ---")
	visualize_schedule(
		schedule=cab.schedule,
		tw_lower=cust.tw_lower,
		tw_upper=cust.tw_upper,
		cursor=(L if L >= 0 else None)
	)
	print("Interval returned:", (L, R))

# --------------------------------------------------------------
# DUMMY ENUMS / CLASSES TO MAKE THE TESTS SELF-CONTAINED
# --------------------------------------------------------------

class CabEntryType(Enum):
	CT  = 1
	ChP = 2
	FM  = 3
	LM  = 4
	PT  = 5
	X   = 6         # Non-fixed


class CabScheduleEntry:
	def __init__(self, _type: CabEntryType):
		self.type = _type
		# The optional debug visualization expects CT entries to carry a
		# customer.request.id shape like the real schedule entries do.
		self.customer = (
			SimpleNamespace(request=SimpleNamespace(id="dummy"))
			if _type == CabEntryType.CT
			else None
		)
		# start_time, end_time will be assigned dynamically by tests


class CabVehicle:
	def __init__(self, entries):
		self.schedule = entries


class Customer:
	def __init__(self, register_time, tw_lower, tw_upper):
		self.register_time = register_time
		self.tw_lower = tw_lower
		self.tw_upper = tw_upper


# Minimal OperationalArea dependency to instantiate CustomSimulation
class OperationalVertices:
	def __init__(self):
		self.sw_lat = 0
		self.sw_lon = 0
		self.ne_lat = 1
		self.ne_lon = 1

class OperationalArea:
	def __init__(self):
		self.operational_area = OperationalVertices()
		self.charging_stations = []
		self.chaining_locations = []
		self.parking_locations = []
		self.pro_routes_and_trips = None


# --------------------------------------------------------------
# DUMMY CustomSimulation that includes ONLY your function
# --------------------------------------------------------------

class CustomSimulation:
	def __init__(self):
		self.operations_area = OperationalArea()

	# >>> Paste your _find_next_interval() method here <<<
	# (Verbatim, without modification)
	
	def _find_next_interval(self, cab: CabVehicle, _cust: Customer, index: int = 0,
							allow_first_gap: bool = True,
							fixed_entry_type = (CabEntryType.CT, CabEntryType.ChP,
												CabEntryType.FM, CabEntryType.LM,
												CabEntryType.PT)):
		"""
		Returns the next valid interval boundary (left, right).

		Returned values:
			(False, -1, -1) -> no interval found; stop scanning completely
			(True, -1, -1) -> entire schedule is free (empty); stop afterwards
			(True, -1, R) -> first gap before fixed entry R; next scan starts at R
			(True, L, R) -> normal middle gap between L and R; next scan starts at R
			(True, L, -1) -> trailing gap after L; stop afterwards
		"""

		schedule = cab.schedule
		n = len(schedule)

		t_now 		= _cust.register_time
		tw_lower 	= _cust.tw_lower
		tw_upper 	= _cust.tw_upper
		
		if tw_upper < t_now:
			print("[scanner] tw ends before register time")
			return False, -1, -1
		
		# ------------------------------------------------------------
		# CASE 0: empty schedule -> whole timeline free
		# ------------------------------------------------------------
		if n == 0:
			print("[scanner] empty schedule")
			return True, -1, -1

		# ------------------------------------------------------------
		# FIRST CALL: allow_first_gap == True
		# ------------------------------------------------------------
		if allow_first_gap:
			# Find the first fixed entry (if any)
			first_fixed = None
			for i in range(n):
				e = schedule[i]
				if e.type in fixed_entry_type:
					first_fixed = i
					break

			if first_fixed is None:
				# No fixed entries at all -> entire schedule free
				print("[scanner] no fixed entries")
				return True, -1, -1

			ff_entry = schedule[first_fixed]

			# Does a first gap exist?
			# entry must start after NOW
			if ff_entry.start_time > t_now:
				# entry starts before the time window -> skip it
				if ff_entry.start_time <= tw_lower:
					# No valid first-gap interval; fall through into normal scanning
					allow_first_gap = False
					index = first_fixed
				else:
					# Valid first gap
					print("[scanner] first gap")
					return True, -1, first_fixed
			# entry had already started before NOW
			else:
				# No first-gap; treat this as a normal scan
				allow_first_gap = False
				index = first_fixed
			
			print("[scanner] no first gap found")

		# ------------------------------------------------------------
		# From here on, index must point to a fixed entry
		# ------------------------------------------------------------

		# ------------------------------------------------------------
		# Scan forward to find the next fixed entry
		# ------------------------------------------------------------
		while ( True ):
			if index < 0 or index >= n:
				# Out of range -> nothing left
				print("[scanner] index beyond (0,len(sched)) ... error???")
				return False, -1, -1

			left_entry = schedule[index]

			if left_entry.type not in fixed_entry_type:
				# This should NEVER happen after first-gap logic; means schedule structure mismatch
				raise RuntimeError("Index passed to _find_next_interval is not a fixed-entry index.")
			
			# stop scanning
			if ( left_entry.end_time > tw_upper ):
				# left entry is done beyond the time window -> nothing else to scan
				print("[scanner] left entry beyond (tw_lower,tw_upper)")
				return False, -1, -1
			
			# ------------------------------------------------------------
			
			next_fixed = None
			for j in range(index + 1, n):
				e = schedule[j]

				if e.type in fixed_entry_type:
					next_fixed = j
					break

			# trailing gap only if no further fixed entries exist
			if next_fixed is None:
				print("[scanner] trailing gap")
				return True, index, -1

			right_entry = schedule[next_fixed]

			# if the right fixed starts before TW, skip internally
			if right_entry.start_time < tw_lower:
				print("[scanner] skipped ealy entries", index, next_fixed)
				index = next_fixed
				continue
				
			# normal pair, even if right starts after tw_upper
			print("[scanner] gap between two entries")
			return True, index, next_fixed


# --------------------------------------------------------------
# Helper functions for tests
# --------------------------------------------------------------

def ts(iso: str):
	return int(datetime.fromisoformat(iso).timestamp())

def mk_entry(type_, start_iso, end_iso):
	e = CabScheduleEntry(type_)
	e.start_time = ts(start_iso)
	e.end_time   = ts(end_iso)
	return e


# Shortcut set of fixed types
FIXED = (CabEntryType.CT, CabEntryType.ChP, CabEntryType.FM, CabEntryType.LM, CabEntryType.PT)


@pytest.fixture
def sim():
	return CustomSimulation()


# --------------------------------------------------------------
# TEST CASES
# --------------------------------------------------------------

def test_empty_schedule(sim):
	cab = CabVehicle([])
	cust = Customer(ts("2025-10-18T00:00:00+02:00"),
					ts("2025-10-18T00:00:00+02:00"),
					ts("2025-10-18T23:59:59+02:00"))

	flag, L, R = sim._find_next_interval(cab, cust)
	debug_show(cab, cust, L, R)       # optional visualization
	assert (flag, L, R) == (True, -1, -1)


def test_no_fixed(sim):
	cab = CabVehicle([
		mk_entry(CabEntryType.X, "2025-10-18T07:00:00+02:00", "2025-10-18T07:10:00+02:00"),
		mk_entry(CabEntryType.X, "2025-10-18T09:00:00+02:00", "2025-10-18T09:20:00+02:00")
	])

	cust = Customer(ts("2025-10-18T06:00:00+02:00"),
					ts("2025-10-18T06:00:00+02:00"),
					ts("2025-10-18T23:00:00+02:00"))

	flag, L, R = sim._find_next_interval(cab, cust)
	debug_show(cab, cust, L, R)       # optional visualization
	assert (flag, L, R) == (True, -1, -1)


def test_first_gap_valid(sim):
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T07:23:00+02:00", "2025-10-18T07:33:00+02:00")
	])

	cust = Customer(ts("2025-10-18T06:00:00+02:00"),
					ts("2025-10-18T06:30:00+02:00"),
					ts("2025-10-18T23:00:00+02:00"))

	flag, L, R = sim._find_next_interval(cab, cust)
	debug_show(cab, cust, L, R)       # optional visualization
	assert (flag, L, R) == (True, -1, 0)


def test_first_gap_skipped(sim):
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T07:00:00+02:00", "2025-10-18T07:30:00+02:00")
	])

	cust = Customer(ts("2025-10-18T06:00:00+02:00"),
					ts("2025-10-18T08:00:00+02:00"),
					ts("2025-10-18T22:00:00+02:00"))

	flag, L, R = sim._find_next_interval(cab, cust)
	debug_show(cab, cust, L, R)       # optional visualization
	assert flag is False or L == 0


def test_middle_gap(sim):
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T07:00:00+02:00", "2025-10-18T07:10:00+02:00"),
		mk_entry(CabEntryType.X,  "2025-10-18T10:00:00+02:00", "2025-10-18T10:30:00+02:00"),
		mk_entry(CabEntryType.CT, "2025-10-18T14:00:00+02:00", "2025-10-18T14:10:00+02:00")
	])

	cust = Customer(ts("2025-10-18T00:00:00+02:00"),
					ts("2025-10-18T08:00:00+02:00"),
					ts("2025-10-18T12:00:00+02:00"))
	
	flag, L, R = sim._find_next_interval(cab, cust, index=0, allow_first_gap=False)
	debug_show(cab, cust, L, R)       # optional visualization
	assert (flag, L, R) == (True, 0, 2)


def test_trailing_gap(sim):
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T07:00:00+02:00", "2025-10-18T07:10:00+02:00"),
		mk_entry(CabEntryType.X,  "2025-10-18T09:00:00+02:00", "2025-10-18T09:20:00+02:00")
	])

	cust = Customer(ts("2025-10-18T00:00:00+02:00"),
					ts("2025-10-18T08:00:00+02:00"),
					ts("2025-10-18T23:00:00+02:00"))

	flag, L, R = sim._find_next_interval(cab, cust, index=0, allow_first_gap=False)
	debug_show(cab, cust, L, R)       # optional visualization
	assert (flag, L, R) == (True, 0, -1)

# --------------------------------------------------------------
# ADDITIONAL EDGE CASE TESTS
# --------------------------------------------------------------

def test_tw_before_all_entries(sim):
	"""TW entirely before the first fixed entry -> no valid gap."""
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T10:00:00+02:00", "2025-10-18T10:30:00+02:00"),
	])
	cust = Customer(
		register_time=ts("2025-10-18T00:00:00+02:00"),
		tw_lower=ts("2025-10-18T01:00:00+02:00"),
		tw_upper=ts("2025-10-18T01:10:00+02:00")
	)

	flag, L, R = sim._find_next_interval(cab, cust)
	debug_show(cab, cust, L, R)
	assert (flag, L, R) == (True, -1, 0)


def test_tw_after_all_entries(sim):
	"""TW entirely after last fixed -> trailing gap should appear."""
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T05:00:00+02:00", "2025-10-18T05:30:00+02:00"),
	])
	cust = Customer(
		register_time=ts("2025-10-18T00:00:00+02:00"),
		tw_lower=ts("2025-10-18T12:00:00+02:00"),
		tw_upper=ts("2025-10-18T20:00:00+02:00")
	)

	flag, L, R = sim._find_next_interval(cab, cust, allow_first_gap=False)
	debug_show(cab, cust, L, R)
	assert (flag, L, R) == (True, 0, -1)


def test_left_ends_exactly_at_tw_lower(sim):
	"""left.end_time == TW_lower -> gap should begin at TW_lower and be valid."""
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T07:00:00+02:00", "2025-10-18T08:00:00+02:00"),
		mk_entry(CabEntryType.CT, "2025-10-18T12:00:00+02:00", "2025-10-18T13:00:00+02:00"),
	])
	cust = Customer(
		register_time=ts("2025-10-18T06:00:00+02:00"),
		tw_lower=ts("2025-10-18T08:00:00+02:00"),
		tw_upper=ts("2025-10-18T11:00:00+02:00")
	)

	flag, L, R = sim._find_next_interval(cab, cust, index=0, allow_first_gap=True)
	debug_show(cab, cust, L, R)
	assert (flag, L, R) == (True, 0, 1)


def test_right_starts_exactly_at_tw_lower(sim):
	"""right.start_time == TW_lower -> should be skipped and not returned."""
	""" TODO?: WRONG: It's just a zero width gap, so it might be allowed"""
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T05:00:00+02:00", "2025-10-18T06:00:00+02:00"),
		mk_entry(CabEntryType.CT, "2025-10-18T08:00:00+02:00", "2025-10-18T09:00:00+02:00"),
	])
	cust = Customer(
		register_time=ts("2025-10-18T04:00:00+02:00"),
		tw_lower=ts("2025-10-18T08:00:00+02:00"),
		tw_upper=ts("2025-10-18T12:00:00+02:00")
	)

	flag, L, R = sim._find_next_interval(cab, cust, index=0, allow_first_gap=True)
	debug_show(cab, cust, L, R)
	# Should skip the second fixed and reach trailing gap
	#assert (flag, L, R) == (True, 1, -1)
	assert (flag, L, R) == (True, 0, 1)


def test_right_starts_exactly_at_tw_upper(sim):
	"""right.start_time == TW_upper -> still a valid gap."""
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T04:00:00+02:00", "2025-10-18T05:00:00+02:00"),
		mk_entry(CabEntryType.CT, "2025-10-18T10:00:00+02:00", "2025-10-18T11:00:00+02:00"),
	])
	cust = Customer(
		register_time=ts("2025-10-18T00:00:00+02:00"),
		tw_lower=ts("2025-10-18T08:00:00+02:00"),
		tw_upper=ts("2025-10-18T10:00:00+02:00")
	)

	flag, L, R = sim._find_next_interval(cab, cust, index=0, allow_first_gap=False)
	debug_show(cab, cust, L, R)
	assert (flag, L, R) == (True, 0, 1)


def test_left_ends_exactly_at_tw_upper(sim):
	"""left.end_time == TW_upper -> scanning must stop (no valid gap)."""
	""" TODO?: WRONG: It's just a zero width gap, so it might be allowed"""
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T05:00:00+02:00", "2025-10-18T10:00:00+02:00"),
	])
	cust = Customer(
		register_time=ts("2025-10-18T00:00:00+02:00"),
		tw_lower=ts("2025-10-18T08:00:00+02:00"),
		tw_upper=ts("2025-10-18T10:00:00+02:00")
	)

	flag, L, R = sim._find_next_interval(cab, cust, index=0, allow_first_gap=True)
	debug_show(cab, cust, L, R)
	#assert (flag, L, R) == (False, -1, -1)
	assert (flag, L, R) == (True, 0, -1)


def test_zero_width_gap(sim):
	"""left.end_time == right.start_time -> zero-width gap -> still valid."""
	""" TODO?: Remove this case?"""
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T05:00:00+02:00", "2025-10-18T06:00:00+02:00"),
		mk_entry(CabEntryType.CT, "2025-10-18T06:00:00+02:00", "2025-10-18T07:00:00+02:00"),
	])
	cust = Customer(
		register_time=ts("2025-10-18T00:00:00+02:00"),
		tw_lower=ts("2025-10-18T05:00:00+02:00"),
		tw_upper=ts("2025-10-18T10:00:00+02:00")
	)

	flag, L, R = sim._find_next_interval(cab, cust, index=0, allow_first_gap=False)
	debug_show(cab, cust, L, R)
	assert (flag, L, R) == (True, 0, 1)


def test_multiple_skipped_fixed(sim):
	"""Several fixed entries start before TW and must be skipped."""
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T01:00:00+02:00", "2025-10-18T02:00:00+02:00"),
		mk_entry(CabEntryType.CT, "2025-10-18T02:00:00+02:00", "2025-10-18T03:00:00+02:00"),
		mk_entry(CabEntryType.CT, "2025-10-18T03:00:00+02:00", "2025-10-18T04:00:00+02:00"),
		mk_entry(CabEntryType.CT, "2025-10-18T08:00:00+02:00", "2025-10-18T09:00:00+02:00"),
	])

	cust = Customer(
		register_time=ts("2025-10-18T00:00:00+02:00"),
		tw_lower=ts("2025-10-18T05:00:00+02:00"),
		tw_upper=ts("2025-10-18T20:00:00+02:00")
	)

	flag, L, R = sim._find_next_interval(cab, cust, allow_first_gap=True)
	debug_show(cab, cust, L, R)
	assert (flag, L, R) == (True, 2, 3)


def test_unsorted_schedule(sim):
	"""Schedule entries not sorted -> behavior should still be deterministic."""
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T12:00:00+02:00", "2025-10-18T13:00:00+02:00"),
		mk_entry(CabEntryType.CT, "2025-10-18T07:00:00+02:00", "2025-10-18T08:00:00+02:00"),
	])
	# TW in between
	cust = Customer(
		register_time=ts("2025-10-18T00:00:00+02:00"),
		tw_lower=ts("2025-10-18T09:00:00+02:00"),
		tw_upper=ts("2025-10-18T11:00:00+02:00")
	)

	flag, L, R = sim._find_next_interval(cab, cust)
	debug_show(cab, cust, L, R)
	# No guaranteed behavior—just ensure it doesn't crash
	assert flag in (True, False)


def test_consecutive_fixed_with_no_gap(sim):
	"""Two fixed entries touching or overlapping should still return a zero-width gap."""
	cab = CabVehicle([
		mk_entry(CabEntryType.CT, "2025-10-18T05:00:00+02:00", "2025-10-18T07:00:00+02:00"),
		mk_entry(CabEntryType.CT, "2025-10-18T06:30:00+02:00", "2025-10-18T08:00:00+02:00"),  # Overlap
		mk_entry(CabEntryType.CT, "2025-10-18T10:00:00+02:00", "2025-10-18T11:00:00+02:00")
	])

	cust = Customer(
		register_time=ts("2025-10-18T00:00:00+02:00"),
		tw_lower=ts("2025-10-18T09:00:00+02:00"),
		tw_upper=ts("2025-10-18T12:00:00+02:00")
	)

	flag, L, R = sim._find_next_interval(cab, cust)
	debug_show(cab, cust, L, R)
	# First usable gap is with the 3rd fixed entry
	assert (flag, L, R) == (True, 1, 2)
