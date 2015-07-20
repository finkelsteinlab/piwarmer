function display_step(data) {
    if (data['mode'] == 'set') {
        return 'Set to ' + data['temperature'] + '&deg;C for ' + data['duration'] + ' seconds'
    }
    if (data['mode'] == 'linear') {
        return 'Ramp from ' + data['start_temperature'] + '&deg;C to ' + data['end_temperature'] + '&deg;C over ' + data['duration'] + ' seconds.'
    }
    if (data['mode'] == 'hold') {
        return 'Hold at ' + data['temperature'] + '&deg;C'
    }
    if (data['mode'] == 'repeat') {
        return 'Repeat ' + data['num_repeats'] + ' times'
    }
}

$(document).ready(function(){
    program_id = get_id('id');
    http('program/' + program_id, 'GET', null, function(response) {
        scientist = response['scientist']
        // Set the program name
        $("#program_title").html(response['name']);

        // Set the driver name
        http('driver/' + response['driver'], 'GET', null, function(driver_response){
            $("#driver_name").html("Driver: " + driver_response['name']);
        });

        // Parse the steps and build up the HTML
        step_data = JSON.parse(response['steps']);
        step_keys = [];
        steps = "";
        for (var key in step_data) {
            step_keys.push(key);
        }
        step_keys.sort()
        for (var key in step_keys) {
            steps += '<tr><td>' + step_keys[key] + '</td><td>' + display_step(step_data[step_keys[key]]) + '</td></tr>';
        }
        $("#program_details").html(steps);
    });

    $(document.body).on('click', '#delete', function() {
        if (window.confirm('Are you sure you want to delete this program? This CANNOT be undone.')) {
            http('program/' + program_id, 'DELETE', null, function(response) {
                window.location.href = '/program?user=' + scientist;
            })
        }
    });
});