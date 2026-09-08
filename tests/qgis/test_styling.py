"""Guard auto-styling: the ramp names resolve, and the renderer refuses to guess.

Two failures this file exists to catch, both silent:

* **A ramp name that does not exist.** ``QgsStyle.colorRamp()`` returns
  ``None`` for an unknown name rather than raising, and the names in this build
  are capitalised -- ``'Viridis'``, ``'RdBu'``. Measured: ``colorRamp('viridis')``
  and ``colorRamp('rdbu')`` are both ``None``, while 35 capitalised names
  resolve. A lowercase table would therefore produce an unstyled layer with
  nothing on stderr.
* **A renderer built from nothing to classify.** An all-fill series is a legal
  POWER response (an ocean cell for a land parameter), and so is a flat field.
  Classifying either gives one bucket and a layer that draws blank, so
  ``style_point_layer`` must decline and leave the layer's existing renderer
  alone rather than half-configure one.
"""

from __future__ import annotations

from unittest.mock import patch

from qgis.core import (
    QgsGraduatedSymbolRenderer,
    QgsSingleSymbolRenderer,
    QgsStyle,
    QgsSymbol,
)

from nasa_power.core.display import VARIABLE_CLASSES
from nasa_power.qgis_bridge import styling
from nasa_power.qgis_bridge.layers_point import build_point_layer
from nasa_power.qgis_bridge.styling import (
    CLASS_COUNT,
    INVERTED,
    MAX_POINT_SIZE,
    MIN_POINT_SIZE,
    RAMPS,
    fixed_range,
    ramp_for,
    style_point_layer,
)
from tests.qgis.qgis_case import QgisTestCase, observation

#: Colour ramps shipped with QGIS 4.2.2. Pinned because the fallback in
#: ``ramp_for`` is only meaningful if the catalogue it checks against is real.
RAMP_COUNT = 35


class RampNameTests(QgisTestCase):
    """Every name in the table has to resolve to an actual ramp."""

    def test_this_build_has_the_expected_ramp_catalogue(self) -> None:
        self.assertEqual(RAMP_COUNT, len(QgsStyle.defaultStyle().colorRampNames()))

    def test_every_ramp_in_the_table_resolves(self) -> None:
        for klass, name in RAMPS.items():
            with self.subTest(klass=klass):
                self.assertIsNotNone(QgsStyle.defaultStyle().colorRamp(name))

    def test_lowercase_names_resolve_to_nothing(self) -> None:
        # The trap itself: no exception, just None, and then an unstyled layer.
        for name in ("viridis", "rdbu", "inferno", "blues"):
            with self.subTest(name=name):
                self.assertIsNone(QgsStyle.defaultStyle().colorRamp(name))

    def test_every_variable_class_has_a_ramp(self) -> None:
        # Otherwise a class falls through to the "Viridis" default and every
        # such parameter draws identically.
        for klass in VARIABLE_CLASSES:
            with self.subTest(klass=klass):
                self.assertIn(klass, RAMPS)

    def test_ramp_for_returns_a_resolvable_name_per_class(self) -> None:
        cases = (
            ("ALLSKY_SFC_SW_DWN", "kW-hr/m^2/day", "Inferno"),
            ("T2M", "C", "RdBu"),
            ("PRECTOTCORR", "mm/day", "Blues"),
            ("RH2M", "%", "BuGn"),
            ("WS10M", "m/s", "Viridis"),
            ("PS", "kPa", "PuOr"),
            ("GWETTOP", "1", "Cividis"),
            ("WD10M", "Degrees", "Turbo"),
            ("SOMETHING_NEW", "", "Viridis"),
        )
        for parameter, units, expected in cases:
            with self.subTest(parameter=parameter):
                name, _invert = ramp_for(parameter, units)
                self.assertEqual(expected, name)
                self.assertIsNotNone(QgsStyle.defaultStyle().colorRamp(name))

    def test_temperature_inverts_and_irradiance_does_not(self) -> None:
        # RdBu runs red-to-blue, so temperature has to flip it: warm means warm.
        self.assertEqual(("RdBu", True), ramp_for("T2M", "C"))
        self.assertEqual(("Inferno", False), ramp_for("ALLSKY_SFC_SW_DWN", "W m-2"))
        self.assertEqual({"temperature"}, set(INVERTED))

    def test_an_unknown_ramp_name_falls_back_to_a_real_one(self) -> None:
        # If someone adds a ramp this build does not have, the layer should
        # still be styled -- and the inversion flag must survive the fallback.
        with patch.dict(styling.RAMPS, {"temperature": "NoSuchRamp"}):
            name, invert = ramp_for("T2M", "C")
        self.assertEqual("Viridis", name)
        self.assertTrue(invert)
        self.assertIsNotNone(QgsStyle.defaultStyle().colorRamp(name))


class StylePointLayerTests(QgisTestCase):
    """The graduated renderer, and the two cases where it must not be built."""

    def _layer(self, values, parameter: str = "T2M"):
        # native_value is deliberately offset from value: styling can be asked
        # to classify either column, and identical columns would let a renderer
        # that ignored value_field pass the alternate-field test.
        return build_point_layer(
            [
                observation(
                    day,
                    value,
                    parameter=parameter,
                    native_value=None if value is None else value - 273.15,
                )
                for day, value in enumerate(values, 1)
            ],
            "styled",
        )

    def test_the_pinned_class_count_and_size_range(self) -> None:
        # Every other assertion in this class compares the renderer against the
        # imported constant, so it agrees with whatever the constant says:
        # mutation-checked, CLASS_COUNT 7 -> 5 and 1.6/6.0 -> 3.0/3.0 both left
        # this whole file green. The numbers are a legibility judgement -- seven
        # classes are enough to read a gradient and few enough to tell apart in
        # a legend; 1.6 mm is clickable and 6.0 mm does not merge a dense grid
        # into a blob -- so they are pinned here as literals, once.
        self.assertEqual(7, CLASS_COUNT)
        self.assertEqual(1.6, MIN_POINT_SIZE)
        self.assertEqual(6.0, MAX_POINT_SIZE)
        self.assertLess(MIN_POINT_SIZE, MAX_POINT_SIZE)

    def test_styling_a_second_layer_does_not_reuse_a_flipped_ramp(self) -> None:
        # ramp.invert() mutates the ramp object in place. Measured on this
        # build, QgsStyle.defaultStyle().colorRamp() hands back a fresh copy --
        # inverting one leaves the next call's copy at #ca0020 @ 0.0 -- so this
        # is safe. If it ever returned the shared object instead, inverting it
        # would flip RdBu for every layer in the QGIS session and every second
        # POWER temperature layer would be coloured backwards. The test-order
        # coincidence that already hides this (an even number of prior
        # inversions restores the original) is exactly why it is asserted
        # directly rather than left to the suite's alphabetical luck.
        values = [260.0 + 2 * n for n in range(14)]
        first = self._layer(values)
        second = self._layer(values)
        style_point_layer(first, "T2M", "C")
        style_point_layer(second, "T2M", "C")

        # ranges() is bound to a name first, and that is not style. Measured:
        # QgsRendererRange.symbol() returns a pointer borrowed from the range,
        # so writing layer.renderer().ranges()[0].symbol().color().name() as
        # one chain frees the range before the colour is read and yields
        # #000000 -- silently, with no crash and no warning. Binding either the
        # list or the range keeps it alive. Every colour assertion in this file
        # binds `ranges` for that reason.
        first_ranges = first.renderer().ranges()
        second_ranges = second.renderer().ranges()
        self.assertEqual("#0571b0", first_ranges[0].symbol().color().name())
        self.assertEqual("#0571b0", second_ranges[0].symbol().color().name())
        # And the style catalogue itself is unharmed for everything else in the
        # session: RdBu still runs red-to-blue.
        self.assertEqual(
            "#ca0020", QgsStyle.defaultStyle().colorRamp("RdBu").color(0.0).name()
        )

    def test_a_normal_layer_gets_a_graduated_renderer(self) -> None:
        layer = self._layer([260.0 + 2 * n for n in range(14)])
        self.assertTrue(style_point_layer(layer, "T2M", "C"))

        renderer = layer.renderer()
        self.assertIsInstance(renderer, QgsGraduatedSymbolRenderer)
        self.assertEqual("value", renderer.classAttribute())
        self.assertEqual(CLASS_COUNT, len(renderer.ranges()))
        # EqualInterval, never Continuous: Continuous ignores the class count
        # (asked for 11, produced 5).
        self.assertEqual("EqualInterval", renderer.classificationMethod().id())

    def test_classes_span_the_data_and_do_not_overlap(self) -> None:
        layer = self._layer([260.0 + 2 * n for n in range(14)])
        style_point_layer(layer, "T2M", "C")
        ranges = layer.renderer().ranges()
        self.assertAlmostEqual(260.0, ranges[0].lowerValue(), places=6)
        self.assertAlmostEqual(286.0, ranges[-1].upperValue(), places=6)
        for lower, upper in zip(ranges, ranges[1:]):
            self.assertAlmostEqual(lower.upperValue(), upper.lowerValue(), places=9)

    def test_size_gradient_runs_with_the_colour_gradient(self) -> None:
        # Size as well as colour, so the layer survives greyscale printing and
        # a colour-blind reader.
        layer = self._layer([260.0 + 2 * n for n in range(14)])
        style_point_layer(layer, "T2M", "C")
        ranges = layer.renderer().ranges()
        self.assertAlmostEqual(MIN_POINT_SIZE, ranges[0].symbol().size(), places=6)
        self.assertAlmostEqual(MAX_POINT_SIZE, ranges[-1].symbol().size(), places=6)

    def test_inversion_reaches_the_symbols(self) -> None:
        # Measured on this build: RdBu is #ca0020 at 0.0 and #0571b0 at 1.0, so
        # an inverted temperature ramp puts blue on the coldest class and red on
        # the warmest. Asserting the colours is the only way to see that
        # ramp.invert() actually made it into the renderer.
        layer = self._layer([260.0 + 2 * n for n in range(14)])
        style_point_layer(layer, "T2M", "C")
        ranges = layer.renderer().ranges()
        self.assertEqual("#0571b0", ranges[0].symbol().color().name())
        self.assertEqual("#ca0020", ranges[-1].symbol().color().name())

    def test_an_uninverted_ramp_keeps_its_own_direction(self) -> None:
        # Inferno: near-black at 0.0, pale yellow at 1.0. Dark low, bright high
        # is already the right sense for irradiance.
        layer = self._layer(
            [100.0 + 5 * n for n in range(14)], parameter="ALLSKY_SFC_SW_DWN"
        )
        style_point_layer(layer, "ALLSKY_SFC_SW_DWN", "kW-hr/m^2/day")
        ranges = layer.renderer().ranges()
        self.assertEqual("#000004", ranges[0].symbol().color().name())
        self.assertEqual("#fcffa4", ranges[-1].symbol().color().name())

    def test_an_all_fill_series_is_declined_and_changes_nothing(self) -> None:
        # A whole series of fill is a legal POWER response, not an error.
        layer = self._layer([None] * 6)
        marker = QgsSingleSymbolRenderer(QgsSymbol.defaultSymbol(layer.geometryType()))
        layer.setRenderer(marker)

        self.assertFalse(style_point_layer(layer, "T2M", "C"))
        self.assertIs(marker, layer.renderer())
        self.assertNotIsInstance(layer.renderer(), QgsGraduatedSymbolRenderer)

    def test_a_single_distinct_value_is_declined(self) -> None:
        # Equal-interval classification of one value gives one bucket, which
        # draws as a blank layer with a one-line legend.
        layer = self._layer([288.15] * 6)
        marker = QgsSingleSymbolRenderer(QgsSymbol.defaultSymbol(layer.geometryType()))
        layer.setRenderer(marker)

        self.assertFalse(style_point_layer(layer, "T2M", "C"))
        self.assertIs(marker, layer.renderer())

    def test_one_real_value_among_fill_is_declined(self) -> None:
        layer = self._layer([None, 288.15, None, None])
        self.assertFalse(style_point_layer(layer, "T2M", "C"))

    def test_values_differing_below_the_rounding_tolerance_count_as_one(self) -> None:
        # Distinctness is judged at 9 decimal places, so float noise does not
        # produce seven classes that all draw the same colour.
        layer = self._layer([5.0, 5.0000000001])
        self.assertFalse(style_point_layer(layer, "T2M", "C"))

    def test_fill_is_excluded_from_the_classified_range(self) -> None:
        # If -999.0 had survived as a value the whole ramp would collapse onto
        # it; if NULL were read as 0.0 the low class would start at zero.
        layer = self._layer([270.0, None, 280.0, None, 290.0])
        self.assertTrue(style_point_layer(layer, "T2M", "C"))
        ranges = layer.renderer().ranges()
        self.assertAlmostEqual(270.0, ranges[0].lowerValue(), places=6)
        self.assertAlmostEqual(290.0, ranges[-1].upperValue(), places=6)

    def test_an_alternate_value_field_can_be_classified(self) -> None:
        # native_value holds POWER's own numbers, in POWER's own units.
        layer = self._layer([260.0 + 2 * n for n in range(14)])
        self.assertTrue(
            style_point_layer(layer, "T2M", "C", value_field="native_value")
        )
        self.assertEqual("native_value", layer.renderer().classAttribute())


class FixedRangeTests(QgisTestCase):
    """One stretch for the whole animation, so the scale stops chasing the data."""

    def test_all_fill_has_no_range(self) -> None:
        self.assertIsNone(fixed_range([None, None, None]))
        self.assertIsNone(fixed_range([]))

    def test_a_flat_field_gets_a_non_degenerate_range(self) -> None:
        low, high = fixed_range([3.0, 3.0, 3.0])
        self.assertLess(low, 3.0)
        self.assertGreater(high, 3.0)
        # 1% of the value either side.
        self.assertAlmostEqual(2.97, low, places=9)
        self.assertAlmostEqual(3.03, high, places=9)

    def test_a_flat_field_of_zeros_still_gets_a_range(self) -> None:
        # abs(0.0) * 0.01 is 0.0, which would be degenerate again, so the pad
        # falls back to 1.0.
        self.assertEqual((-1.0, 1.0), fixed_range([0.0, 0.0]))

    def test_the_true_extremes_survive_the_fill(self) -> None:
        self.assertEqual((1.0, 9.0), fixed_range([5.0, None, 1.0, 9.0, None]))

    def test_a_negative_range_is_kept_in_order(self) -> None:
        self.assertEqual((-40.0, -1.5), fixed_range([-1.5, -40.0, -20.0]))
