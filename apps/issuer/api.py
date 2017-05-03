import urlparse

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import UploadedFile
from rest_framework import status, authentication
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

import badgrlog
from badgeuser.models import CachedEmailAddress
from entity.api import BaseEntityListView, BaseEntityDetailView
from issuer.api_v1 import AbstractIssuerAPIEndpoint
from issuer.models import Issuer, IssuerStaff, BadgeClass, BadgeInstance
from issuer.permissions import (MayIssueBadgeClass, MayEditBadgeClass,
                                IsEditor, IsStaff, IsOwnerOrStaff, ApprovedIssuersOnly)
from issuer.serializers_v1 import (IssuerSerializerV1, BadgeClassSerializerV1,
                                   BadgeInstanceSerializer, IssuerRoleActionSerializerV1,
                                   IssuerStaffSerializerV1)
from issuer.serializers_v2 import IssuerSerializerV2, BadgeClassSerializerV2
from issuer.utils import get_badgeclass_by_identifier
from mainsite.permissions import AuthenticatedWithVerifiedEmail


logger = badgrlog.BadgrLogger()


class IssuerList(BaseEntityListView):
    """
    Issuer list resource for the authenticated user
    """
    model = Issuer
    v1_serializer_class = IssuerSerializerV1
    v2_serializer_class = IssuerSerializerV2
    permission_classes = (AuthenticatedWithVerifiedEmail, IsEditor, ApprovedIssuersOnly)

    create_event = badgrlog.IssuerCreatedEvent

    def get_objects(self, request, **kwargs):
        return self.request.user.cached_issuers()


class IssuerDetail(BaseEntityDetailView):
    """
    GET details on one issuer.
    """
    model = Issuer
    v1_serializer_class = IssuerSerializerV1
    v2_serializer_class = IssuerSerializerV2
    permission_classes = (AuthenticatedWithVerifiedEmail, IsEditor)


class AllBadgeClassesList(BaseEntityListView):
    """
    GET a list of badgeclasses within one issuer context or
    POST to create a new badgeclass within the issuer context
    """
    model = BadgeClass
    permission_classes = (AuthenticatedWithVerifiedEmail,)
    v1_serializer_class = BadgeClassSerializerV1
    v2_serializer_class = BadgeClassSerializerV2

    def get_objects(self, request, **kwargs):
        return request.user.cached_badgeclasses()

    def get(self, request, **kwargs):
        """
        GET a list of badgeclasses the user has access to
        """
        return super(AllBadgeClassesList, self).get(request, **kwargs)


class BadgeClassList(AbstractIssuerAPIEndpoint):
    """
    GET a list of badgeclasses within one issuer context or
    POST to create a new badgeclass within the issuer context
    """
    queryset = Issuer.objects.all()
    model = Issuer
    permission_classes = (AuthenticatedWithVerifiedEmail, IsEditor,)

    def get(self, request, issuerSlug):
        """
        GET a list of badgeclasses within one Issuer context.
        Authenticated user must have owner, editor, or staff status on Issuer
        ---
        serializer: BadgeClassSerializer
        """
        # Ensure current user has permissions on current issuer
        current_issuer = self.get_list(issuerSlug)

        if not current_issuer.exists():
            return Response(
                "Issuer %s not found or inadequate permissions." % issuerSlug,
                status=status.HTTP_404_NOT_FOUND
            )

        issuer_badge_classes = current_issuer[0].badgeclasses.all()

        if not issuer_badge_classes.exists():
            return Response([], status=status.HTTP_200_OK)

        serializer = BadgeClassSerializerV1(issuer_badge_classes, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request, issuerSlug):
        """
        Define a new BadgeClass to be owned by a particular Issuer.
        Authenticated user must have owner or editor status on Issuer
        ('staff' status is inadequate)
        ---
        serializer: BadgeClassSerializer
        parameters:
            - name: issuerSlug
              required: true
              type: string
              paramType: path
              description: slug of the Issuer to be owner of the new BadgeClass
            - name: name
              required: true
              type: string
              paramType: form
              description: A short name for the new BadgeClass
            - name: slug
              required: false
              type: string
              paramType: form
              description: Optionally customizable slug. Otherwise generated from name
            - name: image
              type: file
              required: true
              paramType: form
              description: An image to represent the BadgeClass. Must be a square PNG with no existing OBI assertion data baked into it.
            - name: criteria
              type: string
              required: true
              paramType: form
              description: Either a URL of a remotely hosted criteria page or a text string describing the criteria.
            - name: description
              type: string
              required: true
              paramType: form
              description: The description of the Badge Class.
        """

        # Step 1: Locate the issuer
        current_issuer = self.get_object(issuerSlug)

        if current_issuer is None:
            return Response(
                "Issuer %s not found or inadequate permissions." % issuerSlug,
                status=status.HTTP_404_NOT_FOUND
            )

        # Step 2: validate, create new Badge Class
        serializer = BadgeClassSerializerV1(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        serializer.save(
            issuer=current_issuer,
            created_by=request.user,
        )
        badge_class = serializer.data

        logger.event(badgrlog.BadgeClassCreatedEvent(badge_class, request.data.get('image')))
        return Response(badge_class, status=status.HTTP_201_CREATED)


class BadgeClassDetail(AbstractIssuerAPIEndpoint):
    """
    GET details on one BadgeClass. PUT and DELETE should be restricted to BadgeClasses that haven't been issued yet.
    """
    queryset = BadgeClass.objects.all()
    model = BadgeClass
    permission_classes = (AuthenticatedWithVerifiedEmail, MayEditBadgeClass,)

    def get(self, request, issuerSlug, badgeSlug):
        """
        GET single BadgeClass representation
        ---
        serializer: BadgeClassSerializer
        """

        try:
            current_badgeclass = BadgeClass.cached.get(slug=badgeSlug)
            self.check_object_permissions(self.request, current_badgeclass)
        except (BadgeClass.DoesNotExist, PermissionDenied):
            return Response(
                "BadgeClass %s could not be found, or inadequate permissions." % badgeSlug,
                status=status.HTTP_404_NOT_FOUND
            )
        else:
            serializer = BadgeClassSerializerV1(current_badgeclass, context={'request': request})
            return Response(serializer.data)

    def delete(self, request, issuerSlug, badgeSlug):
        """
        DELETE a badge class that has never been issued. This will fail if any assertions exist for the BadgeClass.
        Restricted to owners or editors (not staff) of the corresponding Issuer.
        ---
        responseMessages:
            - code: 400
              message: Badge Class either couldn't be deleted. It may have already been issued, or it may already not exist.
            - code: 200
              message: Badge has been deleted.
        """

        try:
            current_badgeclass = BadgeClass.cached.get(slug=badgeSlug)
            self.check_object_permissions(self.request, current_badgeclass)
        except (BadgeClass.DoesNotExist, PermissionDenied):
            return Response(status=status.HTTP_404_NOT_FOUND)
        else:
            if current_badgeclass.recipient_count() > 0:
                return Response("Badge could not be deleted. It has already been issued at least once.", status=status.HTTP_400_BAD_REQUEST)
            elif current_badgeclass.pathway_element_count() > 0:
                return Response("Badge could not be deleted. It is being used as a pathway completion requirement.", status=status.HTTP_400_BAD_REQUEST)
            elif len(current_badgeclass.cached_completion_elements()) > 0:
                return Response("Badge could not be deleted. It is being used as a pathway completion badge.", status=status.HTTP_400_BAD_REQUEST)
            else:
                old_badgeclass = current_badgeclass.json
                current_badgeclass.delete()
                logger.event(badgrlog.BadgeClassDeletedEvent(old_badgeclass, request.user))
                return Response("Badge " + badgeSlug + " has been deleted.", status.HTTP_200_OK)

    def put(self, request, issuerSlug, badgeSlug):
        """
        Update an existing badge class. Existing BadgeInstances will NOT be updated.
        ---
        serializer: BadgeClassSerializer
        """
        try:
            current_badgeclass = BadgeClass.cached.get(slug=badgeSlug)
            self.check_object_permissions(self.request, current_badgeclass)
        except (BadgeClass.DoesNotExist, PermissionDenied):
            return Response(
                "BadgeClass %s could not be found, or inadequate permissions." % badgeSlug,
                status=status.HTTP_404_NOT_FOUND
            )
        else:
            # If image is neither an UploadedFile nor a data uri, ignore it.
            # Likely to occur if client sends back the image attribute (as a url), unmodified from a GET request
            new_image = request.data.get('image')
            cleaned_data = request.data.copy()
            if not isinstance(new_image, UploadedFile) and urlparse.urlparse(new_image).scheme != 'data':
                cleaned_data.pop('image')

            serializer = BadgeClassSerializerV1(current_badgeclass, data=cleaned_data, context={'request': request})
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)


class BatchAssertions(AbstractIssuerAPIEndpoint):
    queryset = BadgeClass.objects.all()
    model = BadgeClass
    serializer_class = BadgeInstanceSerializer
    permission_classes = (AuthenticatedWithVerifiedEmail, MayIssueBadgeClass,)

    def post(self, request, issuerSlug, badgeSlug):
        """
        POST to issue multiple copies of the same badge to multiple recipients
        ---
        parameters:
            - name: assertions
              required: true
              type: array
              items: {
                serializer: BadgeInstanceSerializer
              }
              paramType: form
              description: a list of assertions to issue
        """

        badgeclass_queryset = self.queryset.filter(issuer__slug=issuerSlug)
        current_badgeclass = self.get_object(badgeSlug, queryset=badgeclass_queryset)

        if current_badgeclass is None:
            return Response(
                "Issuer not found or current user lacks permission to issue this badge.",
                status=status.HTTP_404_NOT_FOUND
            )

        create_notification = request.data.get('create_notification', False)
        def _include_create_notification(a):
            a['create_notification'] = create_notification
            return a
        assertions = map(_include_create_notification, request.data.get('assertions'))

        serializer = BadgeInstanceSerializer(
            data=assertions,
            many=True,
            context={'request': request, 'badgeclass': current_badgeclass}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        for data in serializer.data:
            logger.event(badgrlog.BadgeInstanceCreatedEvent(data, request.user))
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class BadgeInstanceList(AbstractIssuerAPIEndpoint):
    """
    GET a list of assertions per issuer & per badgeclass
    POST to issue a new assertion
    """
    queryset = BadgeClass.objects.all()
    model = BadgeClass
    serializer_class = BadgeInstanceSerializer
    permission_classes = (AuthenticatedWithVerifiedEmail, MayIssueBadgeClass,)

    def post(self, request, issuerSlug, badgeSlug):
        """
        Issue a badge to a single recipient.
        ---
        serializer: BadgeInstanceSerializer
        """
        badgeclass_queryset = self.queryset.filter(issuer__slug=issuerSlug)
        current_badgeclass = self.get_object(badgeSlug, queryset=badgeclass_queryset)

        if current_badgeclass is None:
            return Response(
                "Issuer not found or current user lacks permission to issue this badge.",
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = BadgeInstanceSerializer(
            data=request.data,
            context={'request': request, 'badgeclass': current_badgeclass}
        )
        serializer.is_valid(raise_exception=True)

        serializer.save()

        logger.event(badgrlog.BadgeInstanceCreatedEvent(serializer.data, request.user))
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def get(self, request, issuerSlug, badgeSlug):
        """
        Get a list of all issued assertions for a single BadgeClass.
        ---
        serializer: BadgeInstanceSerializer
        """
        badgeclass_queryset = self.queryset.filter(issuer__slug=issuerSlug) \
            .select_related('badgeinstances')
        # Ensure current user has permissions on current badgeclass
        current_badgeclass = self.get_object(badgeSlug,
                                             queryset=badgeclass_queryset)
        if current_badgeclass is None:
            return Response(
                "BadgeClass %s not found or inadequate permissions." % badgeSlug,
                status=status.HTTP_404_NOT_FOUND
            )

        badge_instances = current_badgeclass.badgeinstances.filter(revoked=False)

        if not badge_instances.exists():
            return Response([], status=status.HTTP_200_OK)

        serializer = BadgeInstanceSerializer(badge_instances, many=True,
                                             context={'request': request})
        return Response(serializer.data)


class IssuerBadgeInstanceList(AbstractIssuerAPIEndpoint):
    """
    Retrieve assertions by a recipient identifier within one issuer
    """
    queryset = Issuer.objects.all().select_related('badgeinstances')
    model = Issuer
    permission_classes = (AuthenticatedWithVerifiedEmail, IsStaff,)

    def get(self, request, issuerSlug):
        """
        Get a list of assertions issued to one recpient by one issuer.
        ---
        serializer: BadgeInstanceSerializer
        parameters:
            - name: issuerSlug
              required: true
              type: string
              paramType: path
              description: slug of the Issuer to search for assertions under
            - name: recipient
              required: false
              type: string
              paramType: query
              description: URL-encoded email address of earner to search by
        """
        current_issuer = self.get_object(issuerSlug)

        if current_issuer is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if request.query_params.get('recipient') is not None:
            instances = current_issuer.badgeinstance_set.filter(
                recipient_identifier=request.query_params.get('recipient'),
                revoked=False)
        else:
            instances = current_issuer.badgeinstance_set.filter(revoked=False)

        serializer = BadgeInstanceSerializer(
            instances, context={'request': request}, many=True
        )

        return Response(serializer.data)


class BadgeInstanceDetail(AbstractIssuerAPIEndpoint):
    """
    Endpoints for (GET)ting a single assertion or revoking a badge (DELETE)
    """
    queryset = BadgeInstance.objects.all()
    model = BadgeInstance
    permission_classes = (AuthenticatedWithVerifiedEmail, MayEditBadgeClass,)

    def get(self, request, issuerSlug, badgeSlug, assertionSlug):
        """
        GET a single assertion's details.
        The assertionSlug URL prameter is the only one that varies the request,
        but the assertion must belong to an issuer owned, edited, or staffed by the
        authenticated user.
        ---
        serializer: BadgeInstanceSerializer
        """
        try:
            current_assertion = BadgeInstance.cached.get(slug=assertionSlug)
        except (BadgeInstance.DoesNotExist, PermissionDenied):
            return Response(status=status.HTTP_404_NOT_FOUND)
        else:
            serializer = BadgeInstanceSerializer(current_assertion, context={'request': request})
            return Response(serializer.data)

    def delete(self, request, issuerSlug, badgeSlug, assertionSlug):
        """
        Revoke an issued badge assertion.
        Limited to Issuer owner and editors (not staff)
        ---
        parameters:
            - name: revocation_reason
              description: A short description of why the badge is to be revoked
              required: true
              type: string
              paramType: form
        responseMessages:
            - code: 200
              message: Assertion has been revoked.
            - code: 400
              message: Assertion is already revoked
            - code: 404
              message: Assertion not found or user has inadequate permissions.
        """
        if request.data.get('revocation_reason') is None:
            raise ValidationError("The parameter revocation_reason is required \
                                  to revoke a badge assertion")
        current_assertion = self.get_object(assertionSlug)
        if current_assertion is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if current_assertion.revoked is True:
            return Response("Assertion is already revoked.",
                            status=status.HTTP_400_BAD_REQUEST)

        current_assertion.revoked = True
        current_assertion.revocation_reason = \
            request.data.get('revocation_reason')
        current_assertion.image.delete()
        current_assertion.save()

        if apps.is_installed('badgebook'):
            try:
                from badgebook.models import BadgeObjectiveAward, LmsCourseInfo
                try:
                    award = BadgeObjectiveAward.cached.get(badge_instance_id=current_assertion.id)
                except BadgeObjectiveAward.DoesNotExist:
                    pass
                else:
                    award.delete()
            except ImportError:
                pass

        logger.event(badgrlog.BadgeAssertionRevokedEvent(current_assertion, request.user))
        return Response(
            "Assertion {} has been revoked.".format(current_assertion.slug),
            status=status.HTTP_200_OK
        )
