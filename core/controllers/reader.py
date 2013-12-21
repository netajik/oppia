# Copyright 2012 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Controllers for the Oppia reader view."""

__author__ = 'Sean Lip'

import feconf
from core.controllers import base
from core.domain import exp_domain
from core.domain import exp_services
from core.domain import skins_services
from core.domain import stats_services
from core.domain import widget_registry
import utils

READER_MODE = 'reader'


class ExplorationPage(base.BaseHandler):
    """Page describing a single exploration."""

    def get(self, exploration_id):
        """Handles GET requests."""
        try:
            exploration = exp_services.get_exploration_by_id(exploration_id)
        except Exception as e:
            raise self.PageNotFoundException(e)

        # An unpublished exploration can only be viewed by admins and the
        # editors of that explorations.
        if not exploration.is_public:
            if not (self.is_admin or self.user_id in exploration.editor_ids):
                raise self.PageNotFoundException

        iframed = (self.request.get('iframed') == 'true')

        self.values.update({
            'content': skins_services.get_skin_html(exploration.default_skin),
            'iframed': iframed,
            'is_public': exploration.is_public,
            'nav_mode': READER_MODE,
        })
        self.render_template(
            'reader/reader_exploration.html', iframe_restriction=None)


class ExplorationHandler(base.BaseHandler):
    """Provides the initial data for a single exploration."""

    def get(self, exploration_id):
        """Populates the data on the individual exploration page."""
        # TODO(sll): Maybe this should send a complete state machine to the
        # frontend, and all interaction would happen client-side?
        try:
            exploration = exp_services.get_exploration_by_id(exploration_id)
        except Exception as e:
            raise self.PageNotFoundException(e)

        init_params = exp_services.get_init_params(exploration_id)
        reader_params = exp_services.update_with_state_params(
            exploration_id,
            exploration.init_state_id,
            reader_params=init_params
        )

        init_state = exploration.init_state
        init_html = exp_services.export_content_to_html(
            exploration_id, init_state.content, reader_params)

        interactive_widget = widget_registry.Registry.get_widget_by_id(
            feconf.INTERACTIVE_PREFIX, init_state.widget.widget_id)
        interactive_html = interactive_widget.get_raw_code(
            init_state.widget.customization_args, reader_params)

        self.values.update({
            'block_number': 0,
            'interactive_html': interactive_html,
            'oppia_html': init_html,
            'params': reader_params,
            'state_history': [exploration.init_state_id],
            'state_id': exploration.init_state_id,
            'title': exploration.title,
        })
        self.render_json(self.values)

        stats_services.EventHandler.record_state_hit(
            exploration_id, exploration.init_state_id, True)


class FeedbackHandler(base.BaseHandler):
    """Handles feedback to readers."""

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    def _append_answer_to_stats_log(
            self, old_state, answer, exploration_id, old_state_id,
            old_params, handler, rule):
        """Append the reader's answer to the statistics log."""
        widget = widget_registry.Registry.get_widget_by_id(
            feconf.INTERACTIVE_PREFIX, old_state.widget.widget_id
        )

        recorded_answer = widget.get_stats_log_html(
            old_state.widget.customization_args, old_params, answer)

        stats_services.EventHandler.record_answer_submitted(
            exploration_id, old_state_id, handler, str(rule), recorded_answer)

    def _get_feedback(self, exploration_id, feedback, params):
        """Gets the HTML with Oppia's feedback."""
        if not feedback:
            return ''
        else:
            feedback_bits = feedback.split('\n')
            return exp_services.export_content_to_html(
                exploration_id,
                [exp_domain.Content('text', '<br>'.join(feedback_bits))],
                params)

    def _append_content(self, exploration_id, sticky, finished, old_params,
                        new_state, state_has_changed, html_output):
        """Appends content for the new state to the output variables."""
        if finished:
            return {}, html_output, ''
        else:
            # Populate new parameters.
            new_params = exp_services.update_with_state_params(
                exploration_id, new_state.id, reader_params=old_params)

            if state_has_changed:
                # Append the content for the new state.
                state_html = exp_services.export_content_to_html(
                    exploration_id, new_state.content, new_params)

                if html_output and state_html:
                    html_output += '<br>'

                html_output += state_html

            interactive_html = '' if sticky else (
                widget_registry.Registry.get_widget_by_id(
                    feconf.INTERACTIVE_PREFIX, new_state.widget.widget_id
                ).get_raw_code(new_state.widget.customization_args, new_params)
            )

            return (new_params, html_output, interactive_html)

    def post(self, exploration_id, state_id):
        """Handles feedback interactions with readers."""
        values = {}

        old_state = exp_services.get_state_by_id(exploration_id, state_id)

        # The reader's answer.
        answer = self.payload.get('answer')
        # The answer handler (submit, click, etc.)
        handler = self.payload.get('handler')
        # The 0-based index of the last content block already on the page.
        block_number = self.payload.get('block_number') + 1
        # Parameters associated with the reader.
        old_params = self.payload.get('params', {})
        old_params['answer'] = answer
        # The reader's state history.
        state_history = self.payload['state_history']

        rule = exp_services.classify(
            exploration_id, state_id, handler, answer, old_params)
        feedback = rule.get_feedback_string()
        new_state_id = rule.dest
        new_state = (
            None if new_state_id == feconf.END_DEST
            else exp_services.get_state_by_id(exploration_id, new_state_id))

        stats_services.EventHandler.record_state_hit(
            exploration_id, new_state_id, (new_state_id not in state_history))
        state_history.append(new_state_id)

        # If the new state widget is the same as the old state widget, and the
        # new state widget is sticky, do not render the reader response. The
        # interactive widget in the frontend should take care of this.
        # TODO(sll): This special-casing is not great; we should
        # make the interface for updating the frontend more generic so that
        # all the updates happen in the same place. Perhaps in the non-sticky
        # case we should call a frontend method named appendFeedback() or
        # similar.
        sticky = (
            new_state_id != feconf.END_DEST and
            new_state.widget.sticky and
            new_state.widget.widget_id == old_state.widget.widget_id
        )

        self._append_answer_to_stats_log(
            old_state, answer, exploration_id, state_id, old_params,
            handler, rule)

        # Append the reader's answer to the response HTML.
        reader_response_html = ''
        reader_response_iframe = ''

        old_widget = widget_registry.Registry.get_widget_by_id(
            feconf.INTERACTIVE_PREFIX, old_state.widget.widget_id)
        reader_response_html, reader_response_iframe = (
            old_widget.get_reader_response_html(
                old_state.widget.customization_args, old_params, answer)
        )
        values['reader_response_html'] = reader_response_html
        values['reader_response_iframe'] = reader_response_iframe

        # Add Oppia's feedback to the response HTML.
        html_output = self._get_feedback(exploration_id, feedback, old_params)

        # Add the content for the new state to the response HTML.
        finished = (new_state_id == feconf.END_DEST)
        state_has_changed = (old_state.id != new_state_id)
        new_params, html_output, interactive_html = (
            self._append_content(
                exploration_id, sticky, finished, old_params, new_state,
                state_has_changed, html_output))

        values.update({
            'interactive_html': interactive_html,
            'exploration_id': exploration_id,
            'state_id': new_state_id,
            'oppia_html': html_output,
            'block_number': block_number,
            'params': new_params,
            'finished': finished,
            'state_history': state_history,
        })

        self.render_json(values)


class ReaderFeedbackHandler(base.BaseHandler):
    """Submits feedback from the reader."""

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    def post(self, exploration_id, state_id):
        """Handles POST requests."""

        feedback = self.payload.get('feedback')
        state_history = self.payload.get('state_history')
        # TODO(sll): Add the reader's history log here.
        stats_services.EventHandler.record_state_feedback_from_reader(
            exploration_id, state_id, feedback,
            {'state_history': state_history})


class RandomExplorationPage(base.BaseHandler):
    """Returns the page for a random exploration."""

    def get(self):
        """Handles GET requests."""
        iframed = (self.request.get('iframed') == 'true')

        explorations = exp_services.get_public_explorations()
        if len(explorations) <= 1:
            exp_services.delete_demo('1')
            exp_services.load_demo('1')
            selected_exploration_id = '1'
        else:
            selected_exploration_id = utils.get_random_choice(
                explorations[1:]).id

        dest_url = '/learn/%s' % selected_exploration_id
        if iframed:
            dest_url += '?iframed=true'
        self.redirect(dest_url)
