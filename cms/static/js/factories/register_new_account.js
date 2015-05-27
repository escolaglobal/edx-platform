define(['jquery', 'jquery.cookie'], function($) {
    'use strict';
    return function () {
        $('form :input')
            .focus(function() {
                $('label[for="' + this.id + '"]').addClass('is-focused');
            })
            .blur(function() {
                $('label').removeClass('is-focused');
            });

        $('form#register_form').submit(function(event) {
            event.preventDefault();
            $('#register_success').stop().removeClass('is-shown');
            var submit_data = $('#register_form').serialize();

            $.ajax({
                url: '/register_new_account',
                type: 'POST',
                dataType: 'json',
                headers: {'X-CSRFToken': $.cookie('csrftoken')},
                notifyOnError: false,
                data: submit_data,
                success: function(json) {
                    $('#register_error').stop().removeClass('is-shown');

                    $('new_registered_user').html('<a href="' + 'http://'+location.hostname+'/u/'+$('#username').val() + '">' + $('#username').val() + '</a>');
                    $('#register_success').stop().addClass('is-shown');
                },
                error: function(jqXHR, textStatus, errorThrown) {
                   var json = $.parseJSON(jqXHR.responseText);
                   $('#register_error').html(json.value).stop().addClass('is-shown');
                }
            });
        });
    };
});
