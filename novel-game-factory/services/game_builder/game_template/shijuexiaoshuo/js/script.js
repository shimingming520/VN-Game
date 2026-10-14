/* global monogatari */

// Define the messages used in the game.
monogatari.action ('message').messages ({
	'Help': {
		title: 'Help',
		subtitle: 'Useful Links',
		body: `
			<p><a href='#'>Engine Documentation</a> - Learn how the visual novel engine works.</p>
			<p><a href='#'>Sample Project</a> - Open a simple demonstration project.</p>
		`
	}
});

// Define the notifications used in the game
monogatari.action ('notification').notifications ({
	'Welcome': {
		title: 'Welcome',
		body: 'Welcome to the visual novel',
		icon: ''
	}
});

// Define the Particles JS Configurations used in the game
monogatari.action ('particles').particles ({

});

// Define the canvas objects used in the game
monogatari.action ('canvas').objects ({

});

// Credits of the people involved in the creation of this awesome game
monogatari.configuration ('credits', {

});


// Define the images that will be available on your game's image gallery
monogatari.assets ('gallery', {

});

// Define the music used in the game.
monogatari.assets ('music', {

});

// Define the voice files used in the game.
monogatari.assets ('voices', {

});

// Define the sounds used in the game.
monogatari.assets ('sounds', {

});

// Define the videos used in the game.
monogatari.assets ('videos', {

});

// Define the images used in the game.
monogatari.assets ('images', {

});

// Define the backgrounds for each scene.
monogatari.assets ('scenes', {

});


// Define the Characters
monogatari.characters ({
	'y': {
		name: 'Guide',
		color: '#5bcaff'
	}
});

monogatari.script ({
	// The game starts here.
	'Start': [
		'show scene #f7f6f6 with fadeIn',
		'show notification Welcome',
		{
			'Input': {
				'Text': 'Enter your name:',
				'Validation': function (input) {
					return input.trim ().length > 0;
				},
				'Save': function (input) {
					this.storage ({
						player: {
							name: input
						}
					});
					return true;
				},
				'Revert': function () {
					this.storage ({
						player: {
							name: ''
						}
					});
				},
				'Warning': 'Please enter a name.'
			}
		},
		'y Hello {{player.name}}! Welcome to the visual novel!',
		{
			'Choice': {
				'Dialog': 'y Have you used a visual novel engine before?',
				'Yes': {
					'Text': 'Yes',
					'Do': 'jump Yes'
				},
				'No': {
					'Text': 'Not yet',
					'Do': 'jump No'
				}
			}
		}
	],

	'Yes': [
		'y Great!',
		'y You are ready to create an amazing visual novel.',
		'y I cannot wait to see the story you tell!',
		'end'
	],

	'No': [

		'y You can start learning now!',

		'show message Help',

		'y Start creating your visual novel!',
		'y I cannot wait to see the story you tell!',
		'end'
	]
});
