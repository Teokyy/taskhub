import unittest

from app.delivery_pricing import calculate_delivery_quote


class DeliveryPricingTests(unittest.TestCase):
    def test_minimum_fare_applies_to_short_trip(self):
        quote = calculate_delivery_quote(0.5, "clear")
        self.assertEqual(quote.total_rub, 213)
        self.assertGreaterEqual(quote.total_rub, 200)

    def test_long_icy_trip_has_weather_surcharge(self):
        quote = calculate_delivery_quote(12, "ice")
        self.assertEqual(quote.total_rub, 800)
        self.assertEqual(quote.weather_factor, 1.6)

    def test_weather_factors_are_not_stacked(self):
        self.assertEqual(calculate_delivery_quote(12, "ice").total_rub, 800)

    def test_invalid_distance_and_weather_are_rejected(self):
        for distance, weather in [(-1, "clear"), (501, "clear"), (float("nan"), "clear"), (5, "storm")]:
            with self.subTest(distance=distance, weather=weather):
                with self.assertRaises(ValueError):
                    calculate_delivery_quote(distance, weather)


if __name__ == "__main__":
    unittest.main()