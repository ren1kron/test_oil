import copy

import pytest

from fuel_planner.data import load_case
from fuel_planner.models import Decisions, Order, Plan, Reservation, day_label


@pytest.fixture
def case():
    return load_case()


@pytest.fixture
def single_year(case):
    c = copy.deepcopy(case)
    c.demand = {2035: c.demand[2035]}
    c.sources = {"A": c.sources["A"]}
    return c


@pytest.fixture
def funded_plan(single_year):
    c = single_year
    demand = c.demand[2035]["base_total_t"]
    loss = c.storage["BASE"]["loss_rate_on_throughput"]
    startup = demand*45/365/(1-loss)
    orders = [Order(source_id="A", order_date="2034-01-01", delivery_date="2035-01-01", volume_t=startup, startup=True)]
    orders += [Order(source_id="A", order_date=day_label(d-365), delivery_date=day_label(d), volume_t=demand/365/(1-loss)) for d in range(365)]
    return Plan(plan_id="test", decisions=Decisions(supply_orders=orders, capacity_reservations=[Reservation(source_id="A", year=2035, annual_capacity_t=sum(o.volume_t for o in orders))]))
