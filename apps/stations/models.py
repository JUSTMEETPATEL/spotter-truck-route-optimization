"""The Station table: one row per OPIS Truckstop ID, with its price spread."""

from django.db import models

from apps.routing.geo import Point


class Station(models.Model):
    """A distinct truckstop, geocoded to its city centroid.

    The mean price is what the optimizer prices fuel at; the min, max and
    sample count stay on the row so the spread behind the mean is visible
    rather than hidden by it.
    """

    opis_id = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    price_mean = models.FloatField()
    price_min = models.FloatField()
    price_max = models.FloatField()
    price_sample_count = models.PositiveIntegerField()

    class Meta:
        ordering = ["state", "city", "name"]
        indexes = [models.Index(fields=["latitude", "longitude"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state})"

    @property
    def point(self) -> Point:
        return Point(self.latitude, self.longitude)
