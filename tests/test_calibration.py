from benchmark.calibrate_confidence import (
    Sample,
    fit_isotonic,
    fit_logistic,
    isotonic_predict,
    logistic_predict,
    metrics,
    split_documents,
)


def test_calibration_pipeline_splits_by_document_and_exports_runtime_values() -> None:
    samples = [
        Sample(
            job_id=f"job-{index}",
            field_path="invoice.invoice_number",
            correct=int(index % 2 == 0),
            features={"mean_logprob": -0.05 if index % 2 == 0 else -4.0, "exact_match": float(index % 2 == 0)},
            critical=True,
            language="English",
            quality_bucket="clean",
        )
        for index in range(20)
    ]
    train, calibration, test = split_documents(samples)
    assert not ({sample.job_id for sample in train} & {sample.job_id for sample in test})
    assert len(train) == 12
    assert len(calibration) == 4
    assert len(test) == 4

    model = fit_logistic(train, iterations=300)
    calibration_probabilities = [logistic_predict(model, sample.features) for sample in calibration]
    mapping = fit_isotonic(calibration_probabilities, [sample.correct for sample in calibration])
    probabilities = [
        isotonic_predict(mapping, logistic_predict(model, sample.features)) for sample in test
    ]
    report = metrics(test, probabilities, baseline_rate=0.5)

    assert model["feature_names"] == ["exact_match", "mean_logprob"]
    assert mapping["x"] and len(mapping["x"]) == len(mapping["y"])
    assert 0 <= report["ece"] <= 1
    assert report["documents"] == 4
