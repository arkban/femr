"""A class and program for converting OMOP v5 sources to femr."""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any, Dict, Mapping, Optional, Sequence

from femr.datasets import RawEvent
from femr.extractors.csv import CSVExtractor

OMOP_BIRTH = 4083587
OMOP_DEATH = 4306655


def get_concept_id(row, field_name):
    source_concept_id = field_name.replace("concept_id", "source_concept_id")
    possib_source = row.get(source_concept_id, "0")
    if possib_source not in ("", "0"):
        return int(possib_source)

    return int(row.get(field_name))


class _DemographicsConverter(CSVExtractor):
    """Convert the OMOP demographics table to events."""

    def get_patient_id_field(self) -> str:
        return "person_id"

    def get_file_prefix(self) -> str:
        return "person"

    def get_events(self, row: Mapping[str, str]) -> Sequence[RawEvent]:
        if row.get("birth_datetime", ""):
            birth = datetime.datetime.fromisoformat(row["birth_datetime"])
        else:
            year = 1900
            month = 1
            day = 1

            if row["year_of_birth"]:
                year = int(row["year_of_birth"])
            else:
                raise RuntimeError("Should always have at least a year of birth?")

            if row["month_of_birth"]:
                month = int(row["month_of_birth"])

            if row["day_of_birth"]:
                day = int(row["day_of_birth"])

            birth = datetime.datetime(year=year, month=month, day=day)

        return [
            # 4216316 is the OMOP birth code
            RawEvent(
                start=birth,
                concept_id=OMOP_BIRTH,
                omop_table="person",
                clarity_table=row.get("load_table_id"),
            )
        ] + [
            RawEvent(
                start=birth,
                concept_id=get_concept_id(row, target),
                omop_table="person",
                clarity_table=row.get("load_table_id"),
            )
            for target in [
                "gender_concept_id",
                "ethnicity_concept_id",
                "race_concept_id",
            ]
            if row[target] != "0"
        ]


def _get_date(row: Mapping[str, str], date_field: str) -> Optional[datetime.datetime]:
    """Extract the highest resolution date from the raw data."""
    for attempt in (date_field + "time", date_field):
        if attempt in row and row[attempt] != "":
            return datetime.datetime.fromisoformat(row[attempt])

    return None


def _try_numeric(val: str) -> float | str | None:
    if val == "":
        return None
    try:
        return float(val)
    except ValueError:
        return val


@dataclasses.dataclass
class _ConceptTableConverter(CSVExtractor):
    """A generic OMOP converter for handling tables that contain a single concept."""

    prefix: str

    file_suffix: str = ""
    concept_id_field: Optional[str] = None
    string_value_field: Optional[str] = None
    numeric_value_field: Optional[str] = None
    concept_id_value_field: Optional[str] = None
    force_concept_id: Optional[int] = None

    def get_patient_id_field(self) -> str:
        return "person_id"

    def get_file_prefix(self) -> str:
        if self.file_suffix:
            return self.prefix + "_" + self.file_suffix
        else:
            return self.prefix

    def get_events(self, row: Mapping[str, str]) -> Sequence[RawEvent]:
        def normalize_to_float_if_possible(field_name: Optional[str], value: str | float | None) -> str | float | None:
            if field_name is not None and field_name in row:
                val = _try_numeric(row[field_name])
                if val is not None:
                    return val
            return value

        value = normalize_to_float_if_possible(self.string_value_field, None)
        value = normalize_to_float_if_possible(self.numeric_value_field, value)

        if self.string_value_field and self.numeric_value_field and self.concept_id_value_field and value is None:
            concept_id_value = _try_numeric(row[self.concept_id_value_field])
            if concept_id_value == 0:
                concept_id_value = None
        else:
            concept_id_value = None

        if self.force_concept_id is not None:
            concept_id = self.force_concept_id
        else:
            concept_id_field = self.concept_id_field or (self.prefix + "_concept_id")
            concept_id = get_concept_id(row, concept_id_field)

        if concept_id_value is not None:
            # This is a very special case. We have a value_as_concept_id with no string or numeric value
            #
            # There are two reasons for this, each with different desired behavior:
            # 1. OMOP defines a code with a maps to value relationship.
            #      See https://www.ohdsi.org/web/wiki/doku.php?id=documentation:vocabulary:mapping
            #      In this cases we generally just want to drop the value, as the data is captured in source_concept_id
            # 2. The ETL has decided to put non-maps to value codes in observation for various reasons.
            #      For instance STARR-OMOP puts shc_medical_hx in here
            #      In this case, we generally want to create a string value with the source code value.
            #
            # As a temporary solution, I am going to just treat these as string features
            # In the future, we might want to explore these as code features
            source_concept_id_column = concept_id_field.replace("_concept_id", "_source_concept_id")
            source_code_column = concept_id_field.replace("_concept_id", "_source_value")
            source_concept_id = row.get(source_concept_id_column) or "0"
            source_code = row.get(source_code_column, "")

            if source_concept_id == "0" and source_code != "":
                # This indicates case #2 above, so we want to set the concept id to the "regular" concept_id and
                # have no value
                concept_id = int(row[concept_id_field])

                # Note that we have to "escape" the source code to avoid integer conversions ..
                value = "SOURCE_CODE/" + source_code
            else:
                # This indicates case #1, so discard the value as it should be retained in source_concept_id correctly
                value = None

        if concept_id == 0:
            # The following are worth recovering even without the code ...
            if self.prefix == "note":
                concept_id = 46235038
            elif self.prefix == "visit":
                concept_id = 8
            elif self.prefix == "visit_detail":
                concept_id = 8
            else:
                return []

        if ((self.prefix + "_start_date") in row) or ((self.prefix + "_start_datetime") in row):
            start = _get_date(row, self.prefix + "_start_date")
            end = _get_date(row, self.prefix + "_end_date")
        else:
            start = _get_date(row, self.prefix + "_date")
            end = None

        if start is None:
            raise RuntimeError("Could not find a date field for " + repr(self) + " " + repr(row))

        if "visit_occurrence_id" in row and row["visit_occurrence_id"]:
            visit_id = int(row["visit_occurrence_id"])
        else:
            visit_id = None

        if "unit_source_value" in row and row["unit_source_value"]:
            unit = row["unit_source_value"]
        else:
            unit = None

        metadata: Dict[str, Any] = {
            "omop_table": self.get_file_prefix(),
            "clarity_table": row.get("load_table_id"),
            "note_id": row.get("note_id"),
        }

        if visit_id is not None:
            metadata["visit_id"] = visit_id

        if end is not None:
            metadata["end"] = end

        if unit is not None:
            metadata["unit"] = unit

        return [RawEvent(start=start, concept_id=concept_id, value=value, **metadata)]


def get_omop_csv_extractors() -> Sequence[CSVExtractor]:
    """Get the list of OMOP Converters."""
    converters = [
        _DemographicsConverter(),
        _ConceptTableConverter(
            prefix="drug_exposure",
            concept_id_field="drug_concept_id",
        ),
        _ConceptTableConverter(
            prefix="visit",
            file_suffix="occurrence",
        ),
        _ConceptTableConverter(
            prefix="condition",
            file_suffix="occurrence",
        ),
        _ConceptTableConverter(
            prefix="death",
            force_concept_id=OMOP_DEATH,
        ),
        _ConceptTableConverter(
            prefix="procedure",
            file_suffix="occurrence",
        ),
        _ConceptTableConverter(prefix="device_exposure", concept_id_field="device_concept_id"),
        _ConceptTableConverter(
            prefix="measurement",
            string_value_field="value_source_value",
            numeric_value_field="value_as_number",
            concept_id_value_field="value_as_concept_id",
        ),
        _ConceptTableConverter(
            prefix="observation",
            string_value_field="value_as_string",
            numeric_value_field="value_as_number",
            concept_id_value_field="value_as_concept_id",
        ),
        _ConceptTableConverter(
            prefix="note",
            concept_id_field="note_class_concept_id",
            string_value_field="note_text",
        ),
        _ConceptTableConverter(
            prefix="visit_detail",
        ),
    ]

    return converters
