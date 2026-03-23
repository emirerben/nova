"""Failure path: 1 of 3 render tasks fails → job status = clips_ready_partial."""


from app.tasks.orchestrate import finalize_job


class TestFinalizeJob:
    def test_all_success_sets_clips_ready(self, mocker):
        mock_session = mocker.MagicMock()
        mock_job = mocker.MagicMock()
        mock_session.__enter__ = mocker.MagicMock(return_value=mock_session)
        mock_session.__exit__ = mocker.MagicMock(return_value=False)
        mock_session.get.return_value = mock_job

        mocker.patch("app.tasks.orchestrate._sync_session", return_value=mock_session)

        results = [
            {"clip_id": "a", "success": True, "error": None},
            {"clip_id": "b", "success": True, "error": None},
            {"clip_id": "c", "success": True, "error": None},
        ]
        finalize_job(results, "00000000-0000-0000-0000-000000000001")
        assert mock_job.status == "clips_ready"

    def test_one_failure_sets_clips_ready_partial(self, mocker):
        mock_session = mocker.MagicMock()
        mock_job = mocker.MagicMock()
        mock_session.__enter__ = mocker.MagicMock(return_value=mock_session)
        mock_session.__exit__ = mocker.MagicMock(return_value=False)
        mock_session.get.return_value = mock_job

        mocker.patch("app.tasks.orchestrate._sync_session", return_value=mock_session)

        results = [
            {"clip_id": "a", "success": True, "error": None},
            {"clip_id": "b", "success": True, "error": None},
            {"clip_id": "c", "success": False, "error": "OOM"},
        ]
        finalize_job(results, "00000000-0000-0000-0000-000000000001")
        assert mock_job.status == "clips_ready_partial"

    def test_all_failures_sets_processing_failed(self, mocker):
        mock_session = mocker.MagicMock()
        mock_job = mocker.MagicMock()
        mock_session.__enter__ = mocker.MagicMock(return_value=mock_session)
        mock_session.__exit__ = mocker.MagicMock(return_value=False)
        mock_session.get.return_value = mock_job

        mocker.patch("app.tasks.orchestrate._sync_session", return_value=mock_session)

        results = [
            {"clip_id": "a", "success": False, "error": "fail"},
            {"clip_id": "b", "success": False, "error": "fail"},
            {"clip_id": "c", "success": False, "error": "fail"},
        ]
        finalize_job(results, "00000000-0000-0000-0000-000000000001")
        assert mock_job.status == "processing_failed"
