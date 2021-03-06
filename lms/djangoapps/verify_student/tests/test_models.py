# -*- coding: utf-8 -*-
from datetime import timedelta, datetime
import ddt
import json
import requests.exceptions
import pytz

from django.conf import settings
from django.test import TestCase
from django.db.utils import IntegrityError
from mock import patch
from nose.tools import assert_is_none, assert_equals, assert_raises, assert_true, assert_false  # pylint: disable=E0611
from opaque_keys.edx.locations import SlashSeparatedCourseKey

from reverification.tests.factories import MidcourseReverificationWindowFactory
from student.tests.factories import UserFactory
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory

from verify_student.models import (
    SoftwareSecurePhotoVerification, VerificationException, VerificationCheckpoint, VerificationStatus,
    SkippedReverification
)

FAKE_SETTINGS = {
    "SOFTWARE_SECURE": {
        "FACE_IMAGE_AES_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "API_ACCESS_KEY": "BBBBBBBBBBBBBBBBBBBB",
        "API_SECRET_KEY": "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
        "RSA_PUBLIC_KEY": """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAu2fUn20ZQtDpa1TKeCA/
rDA2cEeFARjEr41AP6jqP/k3O7TeqFX6DgCBkxcjojRCs5IfE8TimBHtv/bcSx9o
7PANTq/62ZLM9xAMpfCcU6aAd4+CVqQkXSYjj5TUqamzDFBkp67US8IPmw7I2Gaa
tX8ErZ9D7ieOJ8/0hEiphHpCZh4TTgGuHgjon6vMV8THtq3AQMaAQ/y5R3V7Lezw
dyZCM9pBcvcH+60ma+nNg8GVGBAW/oLxILBtg+T3PuXSUvcu/r6lUFMHk55pU94d
9A/T8ySJm379qU24ligMEetPk1o9CUasdaI96xfXVDyFhrzrntAmdD+HYCSPOQHz
iwIDAQAB
-----END PUBLIC KEY-----""",
        "API_URL": "http://localhost/verify_student/fake_endpoint",
        "AWS_ACCESS_KEY": "FAKEACCESSKEY",
        "AWS_SECRET_KEY": "FAKESECRETKEY",
        "S3_BUCKET": "fake-bucket"
    }
}


class MockKey(object):
    """
    Mocking a boto S3 Key object. It's a really dumb mock because once we
    write data to S3, we never read it again. We simply generate a link to it
    and pass that to Software Secure. Because of that, we don't even implement
    the ability to pull back previously written content in this mock.

    Testing that the encryption/decryption roundtrip on the data works is in
    test_ssencrypt.py
    """
    def __init__(self, bucket):
        self.bucket = bucket

    def set_contents_from_string(self, contents):
        self.contents = contents

    def generate_url(self, duration):
        return "http://fake-edx-s3.edx.org/"


class MockBucket(object):
    """Mocking a boto S3 Bucket object."""
    def __init__(self, name):
        self.name = name


class MockS3Connection(object):
    """Mocking a boto S3 Connection"""
    def __init__(self, access_key, secret_key):
        pass

    def get_bucket(self, bucket_name):
        return MockBucket(bucket_name)


def mock_software_secure_post(url, headers=None, data=None, **kwargs):
    """
    Mocks our interface when we post to Software Secure. Does basic assertions
    on the fields we send over to make sure we're not missing headers or giving
    total garbage.
    """
    data_dict = json.loads(data)

    # Basic sanity checking on the keys
    EXPECTED_KEYS = [
        "EdX-ID", "ExpectedName", "PhotoID", "PhotoIDKey", "SendResponseTo",
        "UserPhoto", "UserPhotoKey",
    ]
    for key in EXPECTED_KEYS:
        assert_true(
            data_dict.get(key),
            "'{}' must be present and not blank in JSON submitted to Software Secure".format(key)
        )

    # The keys should be stored as Base64 strings, i.e. this should not explode
    photo_id_key = data_dict["PhotoIDKey"].decode("base64")
    user_photo_key = data_dict["UserPhotoKey"].decode("base64")

    response = requests.Response()
    response.status_code = 200

    return response


def mock_software_secure_post_error(url, headers=None, data=None, **kwargs):
    """
    Simulates what happens if our post to Software Secure is rejected, for
    whatever reason.
    """
    response = requests.Response()
    response.status_code = 400
    return response


def mock_software_secure_post_unavailable(url, headers=None, data=None, **kwargs):
    """Simulates a connection failure when we try to submit to Software Secure."""
    raise requests.exceptions.ConnectionError


# Lots of patching to stub in our own settings, S3 substitutes, and HTTP posting
@patch.dict(settings.VERIFY_STUDENT, FAKE_SETTINGS)
@patch('verify_student.models.S3Connection', new=MockS3Connection)
@patch('verify_student.models.Key', new=MockKey)
@patch('verify_student.models.requests.post', new=mock_software_secure_post)
class TestPhotoVerification(TestCase):

    def test_state_transitions(self):
        """
        Make sure we can't make unexpected status transitions.

        The status transitions we expect are::

                        → → → must_retry
                        ↑        ↑ ↓
            created → ready → submitted → approved
                                    ↓        ↑ ↓
                                    ↓ → →  denied
        """
        user = UserFactory.create()
        attempt = SoftwareSecurePhotoVerification(user=user)
        assert_equals(attempt.status, "created")

        # These should all fail because we're in the wrong starting state.
        assert_raises(VerificationException, attempt.submit)
        assert_raises(VerificationException, attempt.approve)
        assert_raises(VerificationException, attempt.deny)

        # Now let's fill in some values so that we can pass the mark_ready() call
        attempt.mark_ready()
        assert_equals(attempt.status, "ready")

        # ready (can't approve or deny unless it's "submitted")
        assert_raises(VerificationException, attempt.approve)
        assert_raises(VerificationException, attempt.deny)

        DENY_ERROR_MSG = '[{"photoIdReasons": ["Not provided"]}]'

        # must_retry
        attempt.status = "must_retry"
        attempt.system_error("System error")
        attempt.approve()
        attempt.status = "must_retry"
        attempt.deny(DENY_ERROR_MSG)

        # submitted
        attempt.status = "submitted"
        attempt.deny(DENY_ERROR_MSG)
        attempt.status = "submitted"
        attempt.approve()

        # approved
        assert_raises(VerificationException, attempt.submit)
        attempt.approve()  # no-op
        attempt.system_error("System error")  # no-op, something processed it without error
        attempt.deny(DENY_ERROR_MSG)

        # denied
        assert_raises(VerificationException, attempt.submit)
        attempt.deny(DENY_ERROR_MSG)  # no-op
        attempt.system_error("System error")  # no-op, something processed it without error
        attempt.approve()

    def test_name_freezing(self):
        """
        You can change your name prior to marking a verification attempt ready,
        but changing your name afterwards should not affect the value in the
        in the attempt record. Basically, we want to always know what your name
        was when you submitted it.
        """
        user = UserFactory.create()
        user.profile.name = u"Jack \u01B4"  # gratuious non-ASCII char to test encodings

        attempt = SoftwareSecurePhotoVerification(user=user)
        user.profile.name = u"Clyde \u01B4"
        attempt.mark_ready()

        user.profile.name = u"Rusty \u01B4"

        assert_equals(u"Clyde \u01B4", attempt.name)

    def create_and_submit(self):
        """Helper method to create a generic submission and send it."""
        user = UserFactory.create()
        attempt = SoftwareSecurePhotoVerification(user=user)
        user.profile.name = u"Rust\u01B4"

        attempt.upload_face_image("Just pretend this is image data")
        attempt.upload_photo_id_image("Hey, we're a photo ID")
        attempt.mark_ready()
        attempt.submit()

        return attempt

    def test_fetch_photo_id_image(self):
        user = UserFactory.create()
        orig_attempt = SoftwareSecurePhotoVerification(user=user, window=None)
        orig_attempt.save()

        old_key = orig_attempt.photo_id_key

        window = MidcourseReverificationWindowFactory(
            course_id=SlashSeparatedCourseKey("pony", "rainbow", "dash"),
            start_date=datetime.now(pytz.utc) - timedelta(days=5),
            end_date=datetime.now(pytz.utc) + timedelta(days=5)
        )
        new_attempt = SoftwareSecurePhotoVerification(user=user, window=window)
        new_attempt.save()
        new_attempt.fetch_photo_id_image()
        assert_equals(new_attempt.photo_id_key, old_key)

    def test_submissions(self):
        """Test that we set our status correctly after a submission."""
        # Basic case, things go well.
        attempt = self.create_and_submit()
        assert_equals(attempt.status, "submitted")

        # We post, but Software Secure doesn't like what we send for some reason
        with patch('verify_student.models.requests.post', new=mock_software_secure_post_error):
            attempt = self.create_and_submit()
            assert_equals(attempt.status, "must_retry")

        # We try to post, but run into an error (in this case a newtork connection error)
        with patch('verify_student.models.requests.post', new=mock_software_secure_post_unavailable):
            attempt = self.create_and_submit()
            assert_equals(attempt.status, "must_retry")

    def test_active_for_user(self):
        """
        Make sure we can retrive a user's active (in progress) verification
        attempt.
        """
        user = UserFactory.create()

        # This user has no active at the moment...
        assert_is_none(SoftwareSecurePhotoVerification.active_for_user(user))

        # Create an attempt and mark it ready...
        attempt = SoftwareSecurePhotoVerification(user=user)
        attempt.mark_ready()
        assert_equals(attempt, SoftwareSecurePhotoVerification.active_for_user(user))

        # A new user won't see this...
        user2 = UserFactory.create()
        user2.save()
        assert_is_none(SoftwareSecurePhotoVerification.active_for_user(user2))

        # If it's got a different status, it doesn't count
        for status in ["submitted", "must_retry", "approved", "denied"]:
            attempt.status = status
            attempt.save()
            assert_is_none(SoftwareSecurePhotoVerification.active_for_user(user))

        # But if we create yet another one and mark it ready, it passes again.
        attempt_2 = SoftwareSecurePhotoVerification(user=user)
        attempt_2.mark_ready()
        assert_equals(attempt_2, SoftwareSecurePhotoVerification.active_for_user(user))

        # And if we add yet another one with a later created time, we get that
        # one instead. We always want the most recent attempt marked ready()
        attempt_3 = SoftwareSecurePhotoVerification(
            user=user,
            created_at=attempt_2.created_at + timedelta(days=1)
        )
        attempt_3.save()

        # We haven't marked attempt_3 ready yet, so attempt_2 still wins
        assert_equals(attempt_2, SoftwareSecurePhotoVerification.active_for_user(user))

        # Now we mark attempt_3 ready and expect it to come back
        attempt_3.mark_ready()
        assert_equals(attempt_3, SoftwareSecurePhotoVerification.active_for_user(user))

    def test_user_is_verified(self):
        """
        Test to make sure we correctly answer whether a user has been verified.
        """
        user = UserFactory.create()
        attempt = SoftwareSecurePhotoVerification(user=user)
        attempt.save()

        # If it's any of these, they're not verified...
        for status in ["created", "ready", "denied", "submitted", "must_retry"]:
            attempt.status = status
            attempt.save()
            assert_false(SoftwareSecurePhotoVerification.user_is_verified(user), status)

        attempt.status = "approved"
        attempt.save()
        assert_true(SoftwareSecurePhotoVerification.user_is_verified(user), attempt.status)

    def test_user_has_valid_or_pending(self):
        """
        Determine whether we have to prompt this user to verify, or if they've
        already at least initiated a verification submission.
        """
        user = UserFactory.create()
        attempt = SoftwareSecurePhotoVerification(user=user)

        # If it's any of these statuses, they don't have anything outstanding
        for status in ["created", "ready", "denied"]:
            attempt.status = status
            attempt.save()
            assert_false(SoftwareSecurePhotoVerification.user_has_valid_or_pending(user), status)

        # Any of these, and we are. Note the benefit of the doubt we're giving
        # -- must_retry, and submitted both count until we hear otherwise
        for status in ["submitted", "must_retry", "approved"]:
            attempt.status = status
            attempt.save()
            assert_true(SoftwareSecurePhotoVerification.user_has_valid_or_pending(user), status)

    def test_user_status(self):
        # test for correct status when no error returned
        user = UserFactory.create()
        status = SoftwareSecurePhotoVerification.user_status(user)
        self.assertEquals(status, ('none', ''))

        # test for when one has been created
        attempt = SoftwareSecurePhotoVerification(user=user)
        attempt.status = 'approved'
        attempt.save()

        status = SoftwareSecurePhotoVerification.user_status(user)
        self.assertEquals(status, ('approved', ''))

        # create another one for the same user, make sure the right one is
        # returned
        attempt2 = SoftwareSecurePhotoVerification(user=user)
        attempt2.status = 'denied'
        attempt2.error_msg = '[{"photoIdReasons": ["Not provided"]}]'
        attempt2.save()

        status = SoftwareSecurePhotoVerification.user_status(user)
        self.assertEquals(status, ('approved', ''))

        # now delete the first one and verify that the denial is being handled
        # properly
        attempt.delete()
        status = SoftwareSecurePhotoVerification.user_status(user)
        self.assertEquals(status, ('must_reverify', "No photo ID was provided."))

        # test for correct status for reverifications
        window = MidcourseReverificationWindowFactory()
        reverify_status = SoftwareSecurePhotoVerification.user_status(user=user, window=window)
        self.assertEquals(reverify_status, ('must_reverify', ''))

        reverify_attempt = SoftwareSecurePhotoVerification(user=user, window=window)
        reverify_attempt.status = 'approved'
        reverify_attempt.save()

        reverify_status = SoftwareSecurePhotoVerification.user_status(user=user, window=window)
        self.assertEquals(reverify_status, ('approved', ''))

        reverify_attempt.status = 'denied'
        reverify_attempt.save()

        reverify_status = SoftwareSecurePhotoVerification.user_status(user=user, window=window)
        self.assertEquals(reverify_status, ('denied', ''))

        reverify_attempt.status = 'approved'
        # pylint: disable=protected-access
        reverify_attempt.created_at = SoftwareSecurePhotoVerification._earliest_allowed_date() + timedelta(days=-1)
        reverify_attempt.save()
        reverify_status = SoftwareSecurePhotoVerification.user_status(user=user, window=window)
        message = 'Your {platform_name} verification has expired.'.format(platform_name=settings.PLATFORM_NAME)
        self.assertEquals(reverify_status, ('expired', message))

    def test_display(self):
        user = UserFactory.create()
        window = MidcourseReverificationWindowFactory()
        attempt = SoftwareSecurePhotoVerification(user=user, window=window, status="denied")
        attempt.save()

        # We expect the verification to be displayed by default
        self.assertEquals(SoftwareSecurePhotoVerification.display_status(user, window), True)

        # Turn it off
        SoftwareSecurePhotoVerification.display_off(user.id)
        self.assertEquals(SoftwareSecurePhotoVerification.display_status(user, window), False)

    def test_parse_error_msg_success(self):
        user = UserFactory.create()
        attempt = SoftwareSecurePhotoVerification(user=user)
        attempt.status = 'denied'
        attempt.error_msg = '[{"photoIdReasons": ["Not provided"]}]'
        parsed_error_msg = attempt.parsed_error_msg()
        self.assertEquals("No photo ID was provided.", parsed_error_msg)

    def test_parse_error_msg_failure(self):
        user = UserFactory.create()
        attempt = SoftwareSecurePhotoVerification(user=user)
        attempt.status = 'denied'
        # when we can't parse into json
        bad_messages = {
            'Not Provided',
            '[{"IdReasons": ["Not provided"]}]',
            '{"IdReasons": ["Not provided"]}',
            u'[{"ïḋṚëäṡöṅṡ": ["Ⓝⓞⓣ ⓟⓡⓞⓥⓘⓓⓔⓓ "]}]',
        }
        for msg in bad_messages:
            attempt.error_msg = msg
            parsed_error_msg = attempt.parsed_error_msg()
            self.assertEquals(parsed_error_msg, "There was an error verifying your ID photos.")

    def test_active_at_datetime(self):
        user = UserFactory.create()
        attempt = SoftwareSecurePhotoVerification.objects.create(user=user)

        # Not active before the created date
        before = attempt.created_at - timedelta(seconds=1)
        self.assertFalse(attempt.active_at_datetime(before))

        # Active immediately after created date
        after_created = attempt.created_at + timedelta(seconds=1)
        self.assertTrue(attempt.active_at_datetime(after_created))

        # Active immediately before expiration date
        expiration = attempt.created_at + timedelta(days=settings.VERIFY_STUDENT["DAYS_GOOD_FOR"])
        before_expiration = expiration - timedelta(seconds=1)
        self.assertTrue(attempt.active_at_datetime(before_expiration))

        # Not active after the expiration date
        after = expiration + timedelta(seconds=1)
        self.assertFalse(attempt.active_at_datetime(after))

    def test_verification_for_datetime(self):
        user = UserFactory.create()
        now = datetime.now(pytz.UTC)

        # No attempts in the query set, so should return None
        query = SoftwareSecurePhotoVerification.objects.filter(user=user)
        result = SoftwareSecurePhotoVerification.verification_for_datetime(now, query)
        self.assertIs(result, None)

        # Should also return None if no deadline specified
        query = SoftwareSecurePhotoVerification.objects.filter(user=user)
        result = SoftwareSecurePhotoVerification.verification_for_datetime(None, query)
        self.assertIs(result, None)

        # Make an attempt
        attempt = SoftwareSecurePhotoVerification.objects.create(user=user)

        # Before the created date, should get no results
        before = attempt.created_at - timedelta(seconds=1)
        query = SoftwareSecurePhotoVerification.objects.filter(user=user)
        result = SoftwareSecurePhotoVerification.verification_for_datetime(before, query)
        self.assertIs(result, None)

        # Immediately after the created date, should get the attempt
        after_created = attempt.created_at + timedelta(seconds=1)
        query = SoftwareSecurePhotoVerification.objects.filter(user=user)
        result = SoftwareSecurePhotoVerification.verification_for_datetime(after_created, query)
        self.assertEqual(result, attempt)

        # If no deadline specified, should return first available
        query = SoftwareSecurePhotoVerification.objects.filter(user=user)
        result = SoftwareSecurePhotoVerification.verification_for_datetime(None, query)
        self.assertEqual(result, attempt)

        # Immediately before the expiration date, should get the attempt
        expiration = attempt.created_at + timedelta(days=settings.VERIFY_STUDENT["DAYS_GOOD_FOR"])
        before_expiration = expiration - timedelta(seconds=1)
        query = SoftwareSecurePhotoVerification.objects.filter(user=user)
        result = SoftwareSecurePhotoVerification.verification_for_datetime(before_expiration, query)
        self.assertEqual(result, attempt)

        # Immediately after the expiration date, should not get the attempt
        after = expiration + timedelta(seconds=1)
        query = SoftwareSecurePhotoVerification.objects.filter(user=user)
        result = SoftwareSecurePhotoVerification.verification_for_datetime(after, query)
        self.assertIs(result, None)

        # Create a second attempt in the same window
        second_attempt = SoftwareSecurePhotoVerification.objects.create(user=user)

        # Now we should get the newer attempt
        deadline = second_attempt.created_at + timedelta(days=1)
        query = SoftwareSecurePhotoVerification.objects.filter(user=user)
        result = SoftwareSecurePhotoVerification.verification_for_datetime(deadline, query)
        self.assertEqual(result, second_attempt)


@patch.dict(settings.VERIFY_STUDENT, FAKE_SETTINGS)
@patch('verify_student.models.S3Connection', new=MockS3Connection)
@patch('verify_student.models.Key', new=MockKey)
@patch('verify_student.models.requests.post', new=mock_software_secure_post)
class TestMidcourseReverification(ModuleStoreTestCase):
    """ Tests for methods that are specific to midcourse SoftwareSecurePhotoVerification objects """

    def setUp(self):
        super(TestMidcourseReverification, self).setUp()
        self.course = CourseFactory.create()
        self.user = UserFactory.create()

    def test_user_is_reverified_for_all(self):

        # if there are no windows for a course, this should return True
        self.assertTrue(SoftwareSecurePhotoVerification.user_is_reverified_for_all(self.course.id, self.user))

        # first, make three windows
        window1 = MidcourseReverificationWindowFactory(
            course_id=self.course.id,
            start_date=datetime.now(pytz.UTC) - timedelta(days=15),
            end_date=datetime.now(pytz.UTC) - timedelta(days=13),
        )

        window2 = MidcourseReverificationWindowFactory(
            course_id=self.course.id,
            start_date=datetime.now(pytz.UTC) - timedelta(days=10),
            end_date=datetime.now(pytz.UTC) - timedelta(days=8),
        )

        window3 = MidcourseReverificationWindowFactory(
            course_id=self.course.id,
            start_date=datetime.now(pytz.UTC) - timedelta(days=5),
            end_date=datetime.now(pytz.UTC) - timedelta(days=3),
        )

        # make two SSPMidcourseReverifications for those windows
        attempt1 = SoftwareSecurePhotoVerification(
            status="approved",
            user=self.user,
            window=window1
        )
        attempt1.save()

        attempt2 = SoftwareSecurePhotoVerification(
            status="approved",
            user=self.user,
            window=window2
        )
        attempt2.save()

        # should return False because only 2 of 3 windows have verifications
        self.assertFalse(SoftwareSecurePhotoVerification.user_is_reverified_for_all(self.course.id, self.user))

        attempt3 = SoftwareSecurePhotoVerification(
            status="must_retry",
            user=self.user,
            window=window3
        )
        attempt3.save()

        # should return False because the last verification exists BUT is not approved
        self.assertFalse(SoftwareSecurePhotoVerification.user_is_reverified_for_all(self.course.id, self.user))

        attempt3.status = "approved"
        attempt3.save()

        # should now return True because all windows have approved verifications
        self.assertTrue(SoftwareSecurePhotoVerification.user_is_reverified_for_all(self.course.id, self.user))

    def test_original_verification(self):
        orig_attempt = SoftwareSecurePhotoVerification(user=self.user)
        orig_attempt.save()
        window = MidcourseReverificationWindowFactory(
            course_id=self.course.id,
            start_date=datetime.now(pytz.UTC) - timedelta(days=15),
            end_date=datetime.now(pytz.UTC) - timedelta(days=13),
        )
        midcourse_attempt = SoftwareSecurePhotoVerification(user=self.user, window=window)
        self.assertEquals(midcourse_attempt.original_verification(user=self.user), orig_attempt)

    def test_user_has_valid_or_pending(self):
        window = MidcourseReverificationWindowFactory(
            course_id=self.course.id,
            start_date=datetime.now(pytz.UTC) - timedelta(days=15),
            end_date=datetime.now(pytz.UTC) - timedelta(days=13),
        )

        attempt = SoftwareSecurePhotoVerification(status="must_retry", user=self.user, window=window)
        attempt.save()

        assert_false(SoftwareSecurePhotoVerification.user_has_valid_or_pending(user=self.user, window=window))

        attempt.status = "approved"
        attempt.save()
        assert_true(SoftwareSecurePhotoVerification.user_has_valid_or_pending(user=self.user, window=window))


@ddt.ddt
class VerificationCheckpointTest(ModuleStoreTestCase):
    """Tests for the VerificationCheckpoint model. """

    MIDTERM = "midterm"
    FINAL = "final"

    def setUp(self):
        super(VerificationCheckpointTest, self).setUp()
        self.user = UserFactory.create()
        self.course = CourseFactory.create()

    @ddt.data(MIDTERM, FINAL)
    def test_get_verification_checkpoint(self, check_point):
        """testing class method of VerificationCheckpoint. create the object and then uses the class method to get the
        verification check point.
        """
        # create the VerificationCheckpoint checkpoint
        ver_check_point = VerificationCheckpoint.objects.create(course_id=self.course.id, checkpoint_name=check_point)
        self.assertEqual(
            VerificationCheckpoint.get_verification_checkpoint(self.course.id, check_point),
            ver_check_point
        )

    def test_get_verification_checkpoint_for_not_existing_values(self):
        """testing class method of VerificationCheckpoint. create the object and then uses the class method to get the
        verification check point.
        """
        # create the VerificationCheckpoint checkpoint
        VerificationCheckpoint.objects.create(course_id=self.course.id, checkpoint_name=self.MIDTERM)

        # get verification for not existing checkpoint
        self.assertEqual(VerificationCheckpoint.get_verification_checkpoint(self.course.id, 'abc'), None)

    def test_unique_together_constraint(self):
        """testing the unique together contraint.
        """
        # create the VerificationCheckpoint checkpoint
        VerificationCheckpoint.objects.create(course_id=self.course.id, checkpoint_name=self.MIDTERM)

        # create the VerificationCheckpoint checkpoint with same course id and checkpoint name
        with self.assertRaises(IntegrityError):
            VerificationCheckpoint.objects.create(course_id=self.course.id, checkpoint_name=self.MIDTERM)

    def test_add_verification_attempt_software_secure(self):
        """testing manytomany relationship. adding softwaresecure attempt to the verification checkpoints.
        """
        # adding two check points.
        check_point1 = VerificationCheckpoint.objects.create(course_id=self.course.id, checkpoint_name=self.MIDTERM)
        check_point2 = VerificationCheckpoint.objects.create(course_id=self.course.id, checkpoint_name=self.FINAL)

        # Make an attempt and added to the checkpoint1.
        check_point1.add_verification_attempt(SoftwareSecurePhotoVerification.objects.create(user=self.user))
        self.assertEqual(check_point1.photo_verification.count(), 1)

        # Make an other attempt and added to the checkpoint1.
        check_point1.add_verification_attempt(SoftwareSecurePhotoVerification.objects.create(user=self.user))
        self.assertEqual(check_point1.photo_verification.count(), 2)

        # make new attempt and adding to the checkpoint2
        attempt = SoftwareSecurePhotoVerification.objects.create(user=self.user)
        check_point2.add_verification_attempt(attempt)
        self.assertEqual(check_point2.photo_verification.count(), 1)

        # remove the attempt from checkpoint2
        check_point2.photo_verification.remove(attempt)
        self.assertEqual(check_point2.photo_verification.count(), 0)


@ddt.ddt
class VerificationStatusTest(ModuleStoreTestCase):
    """ Tests for the VerificationStatus model. """

    def setUp(self):
        super(VerificationStatusTest, self).setUp()
        self.user = UserFactory.create()
        self.course = CourseFactory.create()
        self.check_point1 = VerificationCheckpoint.objects.create(course_id=self.course.id, checkpoint_name="midterm")
        self.check_point2 = VerificationCheckpoint.objects.create(course_id=self.course.id, checkpoint_name="final")
        self.dummy_reverification_item_id_1 = 'i4x://{}/{}/edx-reverification-block/related_assessment_1'.format(
            self.course.location.org,
            self.course.location.course
        )
        self.dummy_reverification_item_id_2 = 'i4x://{}/{}/edx-reverification-block/related_assessment_2'.format(
            self.course.location.org,
            self.course.location.course
        )

    @ddt.data('submitted', "approved", "denied", "error")
    def test_add_verification_status(self, status):
        """ Adding verification status using the class method. """

        # adding verification status
        VerificationStatus.add_verification_status(
            checkpoint=self.check_point1,
            user=self.user,
            status=status,
            location_id=self.dummy_reverification_item_id_1
        )

        # getting the status from db
        result = VerificationStatus.objects.filter(checkpoint=self.check_point1)[0]
        self.assertEqual(result.status, status)
        self.assertEqual(result.user, self.user)

    @ddt.data("approved", "denied", "error")
    def test_add_status_from_checkpoints(self, status):
        """ Adding verification status for checkpoints list after submitting sspv. """

        # add initial verification status for checkpoints
        initial_status = "submitted"
        VerificationStatus.add_verification_status(
            checkpoint=self.check_point1,
            user=self.user,
            status=initial_status,
            location_id=self.dummy_reverification_item_id_1
        )
        VerificationStatus.add_verification_status(
            checkpoint=self.check_point2,
            user=self.user,
            status=initial_status,
            location_id=self.dummy_reverification_item_id_2
        )

        # now add verification status for multiple checkpoint points
        VerificationStatus.add_status_from_checkpoints(
            checkpoints=[self.check_point1, self.check_point2], user=self.user, status=status
        )

        # test that verification status entries with new status have been added
        # for both checkpoints and all entries have related 'location_id'.
        result = VerificationStatus.objects.filter(user=self.user, checkpoint=self.check_point1)
        self.assertEqual(len(result), len(self.check_point1.checkpoint_status.all()))
        self.assertEqual(
            list(result.values_list('location_id', flat=True)),
            list(self.check_point1.checkpoint_status.all().values_list('location_id', flat=True))
        )
        result = VerificationStatus.objects.filter(user=self.user, checkpoint=self.check_point2)
        self.assertEqual(len(result), len(self.check_point2.checkpoint_status.all()))
        self.assertEqual(
            list(result.values_list('location_id', flat=True)),
            list(self.check_point2.checkpoint_status.all().values_list('location_id', flat=True))
        )


class SkippedReverificationTest(ModuleStoreTestCase):
    """Tests for the SkippedReverification model. """

    def setUp(self):
        super(SkippedReverificationTest, self).setUp()
        self.user = UserFactory.create()
        self.course = CourseFactory.create()
        self.checkpoint = VerificationCheckpoint.objects.create(course_id=self.course.id, checkpoint_name="midterm")

    def test_add_skipped_attempts(self):
        """adding skipped re-verification object using class method."""

        # adding verification status
        SkippedReverification.add_skipped_reverification_attempt(
            checkpoint=self.checkpoint, user_id=self.user.id, course_id=unicode(self.course.id)
        )

        # getting the status from db
        result = SkippedReverification.objects.filter(course_id=self.course.id)[0]
        self.assertEqual(result.checkpoint, self.checkpoint)
        self.assertEqual(result.user, self.user)
        self.assertEqual(result.course_id, self.course.id)

    def test_unique_constraint(self):
        """adding skipped re-verification with same user and course id will
        raise integrity exception
        """

        # adding verification object
        SkippedReverification.add_skipped_reverification_attempt(
            checkpoint=self.checkpoint, user_id=self.user.id, course_id=unicode(self.course.id)
        )

        with self.assertRaises(IntegrityError):
            SkippedReverification.add_skipped_reverification_attempt(
                checkpoint=self.checkpoint, user_id=self.user.id, course_id=unicode(self.course.id)
            )

        # Create skipped attempt for different user
        user2 = UserFactory.create()
        SkippedReverification.add_skipped_reverification_attempt(
            checkpoint=self.checkpoint, user_id=user2.id, course_id=unicode(self.course.id)
        )

        # getting the status from db
        result = SkippedReverification.objects.filter(user=user2)[0]
        self.assertEqual(result.checkpoint, self.checkpoint)
        self.assertEqual(result.user, user2)
        self.assertEqual(result.course_id, self.course.id)

    def test_check_user_skipped_reverification_exists(self):
        """Checking check_user_skipped_reverification_exists method returns boolean status"""

        # adding verification status
        SkippedReverification.add_skipped_reverification_attempt(
            checkpoint=self.checkpoint, user_id=self.user.id, course_id=unicode(self.course.id)
        )

        self.assertTrue(
            SkippedReverification.check_user_skipped_reverification_exists(course_id=self.course.id, user=self.user)
        )

        user2 = UserFactory.create()
        self.assertFalse(
            SkippedReverification.check_user_skipped_reverification_exists(course_id=self.course.id, user=user2)
        )
