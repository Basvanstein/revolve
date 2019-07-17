#!/usr/bin/env python3
import asyncio
import os
import sys
import random
import logging
import yaml
import shutil

from pyrevolve.custom_logging import logger
from pyrevolve import parser
from pyrevolve.SDF.math import Vector3
from pyrevolve.tol.manage import World
from pyrevolve.evolution import fitness
from pyrevolve.evolution.individual import Individual
from pyrevolve.genotype.plasticoding.crossover.crossover import CrossoverConfig
from pyrevolve.genotype.plasticoding.crossover.standard_crossover import standard_crossover
from pyrevolve.genotype.plasticoding.initialization import random_initialization
from pyrevolve.genotype.plasticoding.mutation.mutation import MutationConfig
from pyrevolve.genotype.plasticoding.mutation.standard_mutation import standard_mutation
from pyrevolve.genotype.plasticoding.plasticoding import PlasticodingConfig
from pyrevolve.revolve_bot.brain import BrainRLPowerSplines
from pyrevolve.util.supervisor.supervisor_multi import DynamicSimSupervisor

ROBOT_BATTERY = 5000
INDIVIDUAL_MAX_AGE = 60*2  # 2 minutes
INDIVIDUAL_MAX_AGE_SIGMA = 1.0
SEED_POPULATION_START = 50
MIN_POP = 20
MAX_POP = 100
Z_SPAWN_DISTANCE = 0.5
LIMIT_X = 4
LIMIT_Y = 4
MATURE_AGE = 30
MATE_DISTANCE = 0.6
MATING_COOLDOWN = 10
COUPLE_MATING_LIMIT = 5
MATING_INCREASE_RATE = 1.0

PLASTICODING_CONF = PlasticodingConfig()
CROSSOVER_CONF = CrossoverConfig(crossover_prob=1.0)
MUTATION_CONF = MutationConfig(mutation_prob=0.8, genotype_conf=PLASTICODING_CONF)
DATA_FOLDER_BASE = os.path.dirname(os.path.realpath(__file__))


def make_folders(base_dirpath):
    assert (os.path.isdir(base_dirpath))
    counter = 0
    while True:
        dirpath = os.path.join(base_dirpath, str(counter))
        if not os.path.exists(dirpath):
            break
        counter += 1

    print(f"CHOSEN EXPERIMENT FOLDER {dirpath}")

    # if os.path.exists(dirpath):
    #     shutil.rmtree(dirpath)
    os.mkdir(dirpath)
    os.mkdir(dirpath + '/genotypes')
    os.mkdir(dirpath + '/phenotypes')
    os.mkdir(dirpath + '/descriptors')

    return dirpath


class OnlineIndividual(Individual):
    def __init__(self, genotype, max_age):
        super().__init__(genotype)
        self.manager = None
        self.max_age = max_age

    @staticmethod
    def clone_from(other):
        self = OnlineIndividual(other.genotype, other.max_age)
        self.phenotype = other.phenotype
        self.manager = other.manager
        self.fitness = other.fitness
        self.parents = other.parents

    def develop(self):
        super().develop()
        # self.phenotype._brain = BrainRLPowerSplines(evaluation_rate=10.0)

    def age(self):
        if self.manager is not None:
            return self.manager.age()
        else:
            return 0.0

    def charge(self):
        if self.manager is not None:
            return self.manager.charge()
        else:
            return 0.0

    def pos(self):
        if self.manager is not None:
            return self.manager.last_position
        else:
            return None

    def starting_position(self):
        if self.manager is not None:
            return self.manager.starting_position
        else:
            return None

    def distance_to(self, other, planar: bool = True):
        """
        Calculates the Euclidean distance from this robot to
        the given vector position.
        :param other: Target for measuring distance
        :type other: Vector3|OnlineIndividual
        :param planar: If true, only x/y coordinates are considered.
        :return: distance to other
        :rtype: float
        """
        my_pos = self.pos()
        other_pos = other if isinstance(other, Vector3) else other.pos()

        diff = my_pos - other_pos
        if planar:
            diff.z = 0

        return diff.norm()

    def mature(self):
        return self.age() > MATURE_AGE

    def _wants_to_mate(self, other, mating_multiplier):
        if not self.mature():
            return False

        if self.distance_to(other) > MATE_DISTANCE * mating_multiplier:
            return False

        if self.manager.last_mate is not None and \
                float(self.manager.last_update - self.manager.last_mate) < MATING_COOLDOWN:
            return False

        mate_count = self.manager.mated_with.get(other.manager.name, 0)
        if mate_count > COUPLE_MATING_LIMIT:
            return False

        return True

    def mate(self, other, mating_distance_multiplier: float):
        """
        Will try to mate with other
        :param other: potential mate
        :type other: OnlineIndividual
        :param mating_distance_multiplier: multiplier for mating distance, default should be 1
        :return: Genotype generated by the mating process, None if no mating happened
        :rtype: Genotype|None
        """
        if not (self._wants_to_mate(other, mating_distance_multiplier)
                and other._wants_to_mate(self, mating_distance_multiplier)):
            return None

        # save the mating
        self.manager.last_mate = self.manager.last_update
        if other.manager.name in self.manager.mated_with:
            self.manager.mated_with[other.manager.name] += 1
        else:
            self.manager.mated_with[other.manager.name] = 1

        genotype = standard_crossover([self.genotype, other.genotype], PLASTICODING_CONF, CROSSOVER_CONF)
        genotype = standard_mutation(genotype, MUTATION_CONF)

        return OnlineIndividual(genotype)

    def export_life_data(self, folder):
        life = {
            'starting_time': float(self.manager.starting_time),
            'age': float(self.age()),
            'charge': self.charge(),
            'start_pos': str(self.starting_position()),
            'last_pos': str(self.pos()),
            'avg_orientation': str(Vector3(self.manager.avg_roll, self.manager.avg_pitch, self.manager.avg_yaw)),
            'avg_pos': str(Vector3(self.manager.avg_x, self.manager.avg_y, self.manager.avg_z)),
            'last_mate': str(self.manager.last_mate),
            'dead': str(self.manager.dead),
        }

        with open(f'{folder}/life_{self.id}.yaml', 'w') as f:
            f.write(str(yaml.dump(life)))

    def export(self, folder):
        self.export_genotype(folder)
        self.export_phenotype(folder)
        self.export_life_data(folder)

    def __repr__(self):
        _id = None
        if self.phenotype is not None:
            _id = self.phenotype.id
        elif self.genotype.id is not None:
            _id = self.genotype.id
        return f'Individual_{_id}({self.age()}, {self.charge()}, {self.pos()})'


def random_spawn_pos():
    return Vector3(
        random.uniform(-LIMIT_X, LIMIT_X),
        random.uniform(-LIMIT_Y, LIMIT_Y),
        Z_SPAWN_DISTANCE
    )


class Population(object):
    def __init__(self, log, data_folder, connection):
        self._log = log
        self._data_folder = data_folder
        self._connection = connection
        self._robots = []
        self._robot_id_counter = 0
        self._mating_multiplier = 1.0
        self._mating_increase_rate = MATING_INCREASE_RATE
        self._recent_children = []
        self._recent_children_start_time = -1.0
        self._recent_children_delta_time = 30.0

    def __len__(self):
        return len(self._robots)

    async def _insert_robot(self, robot, pos: Vector3, life_duration: float):
        robot.update_substrate()
        robot.battery_level = ROBOT_BATTERY

        # Insert the robot in the simulator
        robot_manager = await self._connection.insert_robot(robot, pos, life_duration)
        return robot_manager

    async def _insert_individual(self, individual: OnlineIndividual, pos: Vector3):
        individual.develop()
        individual.manager = await self._insert_robot(individual.phenotype, pos, individual.max_age)
        individual.export(self._data_folder)
        return individual

    def _remove_individual(self, individual: OnlineIndividual):
        self._robots.remove(individual)
        individual.export(self._data_folder)

        self._connection.unregister_robot(individual.manager)
        # await self._connection.delete_robot(individual.manager)

    def _is_pos_occupied(self, pos, distance):
        for robot in self._robots:
            if robot.distance_to(pos) < distance:
                return True
        return False

    class NoPositionFound(Exception):
        pass

    def _free_random_spawn_pos(self, distance=MATE_DISTANCE + 0.1, n_tries=100):
        pos = random_spawn_pos()
        i = 1
        while self._is_pos_occupied(pos, distance):
            i += 1
            if i > n_tries:
                raise self.NoPositionFound()
            pos = random_spawn_pos()
        return pos

    async def _generate_insert_random_robot(self, _id: int):
        # Load a robot from yaml
        genotype = random_initialization(PLASTICODING_CONF, _id)
        individual = OnlineIndividual(genotype, random.gauss(INDIVIDUAL_MAX_AGE, INDIVIDUAL_MAX_AGE_SIGMA))
        return await self._insert_individual(individual, self._free_random_spawn_pos())

    async def seed_initial_population(self, pause_while_inserting: bool):
        """
        Seed a new population
        """
        if pause_while_inserting:
            await self._connection.pause(True)
        await self._connection.reset(rall=True, time_only=True, model_only=False)
        await self.immigration_season(SEED_POPULATION_START)
        if pause_while_inserting:
            await self._connection.pause(False)

    def print_population(self):
        for individual in self._robots:
            self._log.info(f"{individual} "
                           f"battery {individual.manager.charge()} "
                           f"age {individual.manager.age()} "
                           f"fitness is {fitness.online_old_revolve(individual.manager)}")

    async def death_season(self):
        """
        Checks for age in the all population and if it's their time of that (currently based on age)
        """
        for individual in self._robots:
            # if individual.age() > INDIVIDUAL_MAX_AGE:
            if individual.manager.dead:
                self._log.debug(f"Attempting ROBOT DIES OF OLD AGE: {individual}")
                self._remove_individual(individual)
                self._log.info(f"ROBOT DIES OF OLD AGE: {individual}")

    async def immigration_season(self, population_minimum=MIN_POP):
        """
        Generates new random individual that are inserted in our population if the population size is too little
        """
        while len(self._robots) < population_minimum:
            self._robot_id_counter += 1
            self._log.debug(f"Attempting LOW REACHED")
            try:
                individual = await self._generate_insert_random_robot(self._robot_id_counter)
                self._log.info(f"LOW REACHED: inserting new random robot: {individual}")
                self._robots.append(individual)
            except Population.NoPositionFound:
                # Try again with a new individual
                pass

    def adjust_mating_multiplier(self, time):
        if self._recent_children_start_time < 0:
            self._recent_children_start_time = time
            return

        if time - self._recent_children_start_time > self._recent_children_delta_time:
            # Time to update the multiplier!
            self._recent_children_start_time = time
            n = len(self._recent_children)
            if n < 3:
                self._mating_multiplier *= self._mating_increase_rate
                self._log.info(f'NOT ENOUGH CHILDREN, increasing the range to {MATE_DISTANCE * self._mating_multiplier}'
                               f' (multiplier: {self._mating_multiplier})')
            elif n > 10:
                self._mating_multiplier /= self._mating_increase_rate
                self._log.info(f'TOO MANY CHILDREN,   decreasing the range to {MATE_DISTANCE * self._mating_multiplier}'
                               f' (multiplier: {self._mating_multiplier})')

            self._recent_children.clear()

    async def mating_season(self):
        """
        Checks if mating condition are met for all couple of robots. If so, it produces a new robot from crossover.
        That robot is inserted into the population.
        """

        class BreakIt(Exception):
            pass

        try:
            if len(self._robots) > MAX_POP:
                raise BreakIt
            for individual1 in self._robots:
                if not individual1.mature():
                    continue
                for individual2 in self._robots:
                    if len(self._robots) > MAX_POP:
                        raise BreakIt
                    if individual1 is individual2:
                        continue

                    individual3 = individual1.mate(individual2, mating_distance_multiplier=self._mating_multiplier)
                    if individual3 is None:
                        continue

                    self._recent_children.append(individual3)

                    self._robot_id_counter += 1
                    individual3.genotype.id = self._robot_id_counter

                    # pos3 = (individual1.pos() + individual2.pos())/2
                    # pos3.z = Z_SPAWN_DISTANCE
                    try:
                        pos3 = self._free_random_spawn_pos()
                    except Population.NoPositionFound:
                        self._log.info('Space is too crowded! Cannot insert the new individual, giving up.')
                    else:
                        self._robots.append(individual3)
                        self._log.debug(f'Attempting mate between {individual1} and {individual2} generated {individual3}')
                        await self._insert_individual(individual3, pos3)
                        self._log.info(f'MATE!!!! between {individual1} and {individual2} generated {individual3}')

        except BreakIt:
            pass


async def run():
    data_folder = make_folders(DATA_FOLDER_BASE)
    log = logger.create_logger('experiment', handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(data_folder, 'experiment_manager.log'), mode='w')
    ])

    # Set debug level to DEBUG
    log.setLevel(logging.DEBUG)

    # Parse command line / file input arguments
    settings = parser.parse_args()

    # Start Simulator
    if settings.simulator_cmd != 'debug':
        simulator_supervisor = DynamicSimSupervisor(
            world_file=settings.world,
            simulator_cmd=settings.simulator_cmd,
            simulator_args=["--verbose"],
            plugins_dir_path=os.path.join('.', 'build', 'lib'),
            models_dir_path=os.path.join('.', 'models'),
            simulator_name='gazebo'
        )
        await simulator_supervisor.launch_simulator(port=settings.port_start)
        # let there be some time to sync all initial output of the simulator
        await asyncio.sleep(0.1)

    # Connect to the simulator and pause
    connection = await World.create(settings, world_address=('127.0.0.1', settings.port_start))
    await asyncio.sleep(1)

    robot_population = Population(log, data_folder, connection)

    log.info("SEEDING POPULATION STARTED")
    await robot_population.seed_initial_population(pause_while_inserting=True)
    log.info("SEEDING POPULATION FINISHED")

    # Start the main life loop
    while True:
        await robot_population.death_season()
        await robot_population.mating_season()
        await robot_population.immigration_season()
        robot_population.adjust_mating_multiplier(connection.age())

        await asyncio.sleep(0.05)
