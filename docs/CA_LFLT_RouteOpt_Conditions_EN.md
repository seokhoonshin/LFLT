# CA LFLT RouteOpt Conditions

## Trucks · DCs

1. Use only 2 trucks
2. Origin DCs
   - LGC8: 5267 Sierra Ave, Fontana, CA 92336
   - LGC2: 5550 Mango Ave, Rialto, CA 92377
   - LGC7: 5550 Mango Ave, Rialto, CA 92377
3. LGC2 and LGC7 share one address but are separate DCs: a load is picked up at one DC only (switching between LGC2 and LGC7 for a second turn needs no driving)
4. Truck capacity: less than 800 cu ft
5. Shipments that require a pallet (every product group except HA: AC, ES, BS, MS, ...): less than 400 cu ft
6. Shipments that do not require a pallet (HA): less than 800 cu ft
7. The same limits apply when pallet and non-pallet shipments ship together

## Time

8. Maximum on-duty time is 10 hours from the driver's start, including loading and the return to the DC (e.g. 06:00-16:00, or 08:00-18:00)
9. Driver start = first delivery time (08:00) - 90 minutes of morning loading - drive from the DC to the first stop. The DC opens at 06:00, so the driver never starts earlier: for a far first stop the driver starts at 06:00, departs at 07:30 and arrives after the drive (after 08:00)
10. The first delivery is at 08:00 or later
11. Stops in transit: 20 minutes added to every drive between two stops (Advanced settings "Stops in transit")
12. Unloading (service) time: shipment cu ft × 0.1 minutes, summed per stop
13. Lunch: 30 minutes, between 11:00 and 14:00

## Distance

14. One-way distance less than 200 miles
15. Loads in the 150–200 mile range must be confirmed with the carrier in advance
16. No limit on the number of stops

## Two Stop Rule

17. Total one-way distance less than 200 miles
18. Between stops, both transit time and unloading time must be allowed for
19. If two stops are less than 50 miles apart, allow 2 hours in total (1 hour transit + 1 hour unloading)

## Two Turn Rule

20. Two-turn (second trip) capability is required
21. For a two-turn truck, the total round-trip distance is less than 300 miles
22. Allow at least 3 hours between turns (live loading: return and reloading time)
23. Turn 2 departs at the later of (1) return + DC-to-DC drive + 2 hours of afternoon loading, or (2) last delivery of turn 1 + 3 hours
24. Lunch is taken between the turns

## Assignment Priority

25. Priority 1 = existing LFLT, 2 = add-on candidate, 3 = low priority
26. Orders closest to the DC first
27. It should be possible to run the optimization both with and without GRND
28. When GRND is included, GRND has the lowest priority
    1. Optimize everything except GRND first
    2. Then insert GRND only into the remaining time and capacity
    3. Add a GRND shipment only if it is within 5 miles of a stop that is already assigned
    4. Never remove an assigned stop to make room for GRND
    5. All conditions 1–24 still hold after GRND is added

## Delivery Appointment Contact

29. First: the internal contact written in Memo Text
30. Second: the official contact found by searching the address
